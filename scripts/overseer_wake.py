#!/usr/bin/env python3
"""Host-side WAKE wrapper for the WatcherDog overseer (Component B).

A launchd timer runs this every ~120 s. It runs the deterministic health probe
in-process (``overseer_health.build_report``) and invokes a *pluggable* agent
command (``OVERSEER_WAKE_CMD``) **only when the core has genuinely failed** —
with an ``fcntl.flock`` single-flight lock, a keyed/stuck-only cooldown, a
process-group kill on timeout, and a compact, size-bounded wake log. It NEVER
touches Hermes's own setup and NEVER acts on the agent's exit code (logs it only).

Design: the DECISION LOGIC is pure (``run_wake``) — the clock (``now``), the
invoke ``runner``, the ``data_dir`` for state files, the ``env`` knobs, and the
``build_report`` probe are all injectable, so the whole thing is unit-tested with
``tmp_path`` and a fake runner, never spawning a real agent. ``main`` wires the
real subprocess runner / clock / environment / data dir.

Wake decisions (in order):
  1. probe ``code == 0``         → "ok"            (healthy / core mid-recovery)
  2. stuck cooldown active       → "skip:cooldown" (KEYED; dead/wedged bypass it)
  3. ``OVERSEER_WAKE_CMD`` unset → "would-wake"    (no-op until the manager plugs in)
  4. flock already held          → "skip:inflight" (single-flight)
  5. otherwise                   → "woke"          (held flock; stamp; invoke)

The single-flight lock is ``fcntl.flock(LOCK_EX|LOCK_NB)`` on a persistent fd —
NOT an O_EXCL PID-file. flock has no write-gap (there is no "created-but-empty"
window an overlapping tick can mis-read), and the kernel releases it
automatically when the holder dies, so there is no stale-lock / PID-reuse logic
to get wrong. It is acquired ONLY around the stamp+invoke — so a no-op tick
(code 0 / cooldown / would-wake) acquires nothing and leaves no held lock.

Fail toward waking, never crash the timer: a ``build_report`` exception is
synthesized into ``{"healthy": false, "probe_error": <exc>}`` with reason
"probe error"; any other unexpected error is logged and the process still exits 0.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time

# Ensure local package imports work under an arbitrary CWD / launchd sandbox.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import scripts.overseer_health as overseer_health  # noqa: E402
from watcherdog.config import load_config  # noqa: E402

# --- Defaults (env-overridable; read defensively — a bad value → the default) -
_COOLDOWN_MIN_DEFAULT = 30      # stuck-incident wakes only
_TIMEOUT_MIN_DEFAULT = 15       # max agent runtime before kill + lock release
COOLDOWN_PRUNE_S = 24 * 60 * 60  # prune cooldown entries older than ~1 day
LOG_MAX_BYTES = 1 * 1024 * 1024  # truncate the wake log to its last ~1 MB

_LOCK_NAME = "overseer_wake.lock"
_COOLDOWN_NAME = "overseer_wake.cooldowns.json"
_LOG_NAME = "overseer_wake.log"


# --------------------------------------------------------------------------- #
# Knobs (defensive env reads)
# --------------------------------------------------------------------------- #

def _float_env(env, name, default):
    """``float(env[name])`` or ``default`` (blank / missing / unparseable)."""
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _cooldown_s(env):
    return _float_env(env, "OVERSEER_WAKE_COOLDOWN_MIN", _COOLDOWN_MIN_DEFAULT) * 60.0


def _timeout_s(env):
    return _float_env(env, "OVERSEER_WAKE_TIMEOUT_MIN", _TIMEOUT_MIN_DEFAULT) * 60.0


# --------------------------------------------------------------------------- #
# Reason / key derivation (pure)
# --------------------------------------------------------------------------- #

def _reason(report):
    """Map a probe report → ``(reason, key, urgent)``.

    - dead   → ("watcher down", "down", True)
    - wedged → ("wedged",       "wedged", True)
    - stuck  → ("stuck: <bots>", "stuck:<bots>", False) with bots SORTED for a
      stable key (a different bot-set has a different key, so its cooldown is
      independent). ``urgent`` is True for dead/wedged (they bypass cooldown).
    """
    if not report.get("process_alive", True):
        return "watcher down", "down", True
    if report.get("wedged"):
        return "wedged", "wedged", True
    bots = sorted((report.get("flagged_stuck") or {}).get("bots") or [])
    joined = ",".join(bots)
    if joined:
        return f"stuck: {joined}", f"stuck:{joined}", False
    # Unhealthy for some other reason the report didn't name (defensive): still
    # a non-urgent, keyed wake so we don't lose it.
    return "unhealthy", "unhealthy", False


# --------------------------------------------------------------------------- #
# Single-flight lock — fcntl.flock on a persistent fd.
#
# Why flock and not an O_EXCL PID-file: O_EXCL has a write-gap. The winner of
# os.open(O_CREAT|O_EXCL) holds a CREATED-BUT-EMPTY file until it writes its pid;
# a second tick that hits FileExistsError in that window reads "" → int("") →
# ValueError → "stale" → unlinks the live winner's lock and wins too. Result:
# 2-3 live holders under a race, and the first holder's release unlinks the
# second's lock. flock closes BOTH that gap and the PID-reuse hole: the lock is
# the kernel's advisory lock on the OPEN FD (no file-contents parsing), and the
# kernel drops it automatically when the holder process dies — no stale logic.
# --------------------------------------------------------------------------- #

def _lock_path(data_dir):
    return os.path.join(data_dir, _LOCK_NAME)


def _acquire_lock(data_dir):
    """Try to take the single-flight flock. Returns a ``release`` callable on
    success, or ``None`` if another live wrapper already holds it.

    ``fcntl.flock(LOCK_EX|LOCK_NB)`` on a persistent fd: non-blocking, so a
    second wrapper gets ``BlockingIOError`` immediately instead of waiting. The
    fd is kept open for the whole agent runtime (the lock lives as long as the fd
    is open); ``release`` un-flocks and closes it. The pid written inside is
    debug-only context, NOT used for correctness."""
    path = _lock_path(data_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None  # another wrapper holds it → single-flight skip
    # Record our pid inside for human debugging (not load-bearing).
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass

    def _release(_fd=fd):
        try:
            fcntl.flock(_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(_fd)
            except OSError:
                pass
    return _release


# --------------------------------------------------------------------------- #
# Keyed, stuck-only cooldown
# --------------------------------------------------------------------------- #

def _cooldown_path(data_dir):
    return os.path.join(data_dir, _COOLDOWN_NAME)


def _read_cooldowns(data_dir):
    """``{key: last_wake_epoch}`` from disk; ``{}`` on missing/corrupt/non-dict
    (tolerate a bad file — never crash the timer over it)."""
    path = _cooldown_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _within_cooldown(data_dir, now, key, cooldown_s):
    """True iff ``key`` was stamped within the last ``cooldown_s`` seconds."""
    ts = _read_cooldowns(data_dir).get(key)
    return ts is not None and (now - ts) < cooldown_s


def _stamp_cooldown(data_dir, key, now):
    """Record ``now`` as ``key``'s last-wake time, pruning entries older than a
    day on write (size-bounded). Atomic via write-temp-then-rename."""
    data = _read_cooldowns(data_dir)
    data = {k: ts for k, ts in data.items() if (now - ts) < COOLDOWN_PRUNE_S}
    data[key] = now
    path = _cooldown_path(data_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Compact, size-bounded wake log
# --------------------------------------------------------------------------- #

def _truncate_log(path):
    """If the log exceeds the cap, keep only its last ~``LOG_MAX_BYTES`` bytes
    (we learned the 122 MB lesson). Best-effort; never raises."""
    try:
        if os.path.getsize(path) <= LOG_MAX_BYTES:
            return
        with open(path, "rb") as fh:
            fh.seek(-LOG_MAX_BYTES, os.SEEK_END)
            tail = fh.read()
        # Drop a partial leading line so the file starts clean.
        nl = tail.find(b"\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]
        with open(path, "wb") as fh:
            fh.write(tail)
    except OSError:
        pass


def _log_line(data_dir, decision, reason, bots, rc, elapsed_s, now=None):
    """Append one compact line: ``ts decision reason bots rc elapsed_s``, then
    truncate the file to the cap if it grew past it. Best-effort; never raises."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S",
                        time.localtime(now if now is not None else time.time()))
    # Keep fields single-token-ish; reason may contain a space, so put it last-ish
    # but the parse order is positional per the spec (ts decision reason bots ...).
    line = f"{ts} {decision} reason={reason!r} bots={bots} rc={rc} elapsed_s={elapsed_s}\n"
    path = os.path.join(data_dir, _LOG_NAME)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return
    _truncate_log(path)


# --------------------------------------------------------------------------- #
# Real process-GROUP runner (the thin subprocess glue)
# --------------------------------------------------------------------------- #

def _run_group(argv, stdin_json, timeout):
    """Run ``argv`` with ``shell=False``, feeding ``stdin_json`` on stdin, in its
    OWN process group (``start_new_session=True``). On timeout, kill the WHOLE
    group (SIGTERM, then SIGKILL) so no orphaned children survive. Returns
    ``(rc, elapsed_s)``. The command's rc is logged, never acted on."""
    start = time.time()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # new session/process group → killpg targets all
    )
    try:
        proc.communicate(input=stdin_json.encode("utf-8"), timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _killpg(proc, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        rc = proc.returncode if proc.returncode is not None else -signal.SIGKILL
    return rc, round(time.time() - start, 2)


def _killpg(proc, sig):
    """Signal the child's whole process group; fall back to the lone child if the
    group lookup fails. Swallows the 'already dead' races."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


# --------------------------------------------------------------------------- #
# Pure decision logic — the core deliverable
# --------------------------------------------------------------------------- #

def run_wake(cfg, now, *, data_dir, runner=_run_group, env=None,
             build_report=None):
    """Decide and (maybe) invoke the wake command. Pure given the injected seams.

    Returns a dict ``{"decision": ..., "reason": ..., "rc": ...}`` describing the
    outcome (the timer ignores it; tests assert on it). NEVER raises — any
    unexpected error is logged and reported as ``decision == "error"``.
    """
    env = os.environ if env is None else env
    build_report = build_report or overseer_health.build_report
    try:
        return _decide_and_wake(cfg, now, data_dir, runner, env, build_report)
    except Exception as exc:  # noqa: BLE001 — the timer must never crash
        _log_line(data_dir, "error", str(exc), "", "", "", now=now)
        return {"decision": "error", "reason": str(exc), "rc": None}


def _decide_and_wake(cfg, now, data_dir, runner, env, build_report):
    # 1. Run the probe in-process. On failure, synthesize a report so the agent
    #    still gets context, and force a wake (fail toward waking).
    try:
        report, code = build_report(cfg, now)
    except Exception as exc:  # noqa: BLE001
        report = {"healthy": False, "probe_error": str(exc)}
        code, reason, key, urgent = 1, "probe error", "probe-error", True
    else:
        if code == 0:
            _log_line(data_dir, "ok", "healthy", "", "", "", now=now)
            return {"decision": "ok", "reason": None, "rc": None}
        reason, key, urgent = _reason(report)

    bots = ",".join(sorted((report.get("flagged_stuck") or {}).get("bots") or []))

    # NON-INVOKING decisions FIRST, so they acquire NO lock and leave no held
    # flock behind (preserves the "code0 / cooldown / would-wake create no lock
    # state" guarantee). Single-flight is taken last, guarding ONLY the wake.

    # 2. Keyed, stuck-only cooldown. Dead/wedged are urgent → bypass entirely.
    if not urgent and _within_cooldown(data_dir, now, key, _cooldown_s(env)):
        _log_line(data_dir, "skip:cooldown", reason, bots, "", "", now=now)
        return {"decision": "skip:cooldown", "reason": reason, "rc": None}

    # 3. No command plugged in yet → no-op (safe to install + verify the trigger).
    cmd = (env.get("OVERSEER_WAKE_CMD") or "").strip()
    if not cmd:
        _log_line(data_dir, "would-wake", reason, bots, "", "", now=now)
        return {"decision": "would-wake", "reason": reason, "rc": None}

    # 4. Single-flight: take the flock (LOCK_EX|LOCK_NB) ONLY now, around the
    #    actual wake. None → another wrapper already holds it (a previous wake is
    #    still running) → skip. Held across the whole agent runtime; released in
    #    `finally` on every path (timeout / kill / exception).
    release = _acquire_lock(data_dir)
    if release is None:
        _log_line(data_dir, "skip:inflight", reason, bots, "", "", now=now)
        return {"decision": "skip:inflight", "reason": reason, "rc": None}
    try:
        _stamp_cooldown(data_dir, key, now)
        argv = shlex.split(cmd) + [reason]
        stdin_json = json.dumps(report)
        rc, elapsed = runner(argv, stdin_json, _timeout_s(env))
        _log_line(data_dir, "woke", reason, bots, rc, elapsed, now=now)
        return {"decision": "woke", "reason": reason, "rc": rc}
    finally:
        release()


def main(argv=None):
    cfg = load_config()
    data_dir = os.path.dirname(cfg.db_path) or "."
    run_wake(cfg, time.time(), data_dir=data_dir)
    return 0  # never let a wake outcome flip the timer's exit code


if __name__ == "__main__":
    sys.exit(main())
