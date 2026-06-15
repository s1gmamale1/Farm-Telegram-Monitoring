#!/usr/bin/env python3
"""Standalone health/trigger probe for the WatcherDog overseer (Option B).

Emits ONE compact JSON object describing whether the watcher is healthy and, if
not, why — then exits 0 (healthy) or 1 (a wake-trigger holds). A host-side
launchd/cron job runs this every minute or two and wakes the Hermes overseer
agent ONLY on a nonzero exit.

It deliberately does NOT talk to the overseer socket: the socket dies with the
watcher process, so the signal that matters most (the watcher is down) cannot
come from a socket call. Everything here is read locally — the process table,
the health-beacon mtime, the incidents SQLite, and the tail of the logs.

Wake-triggers (nonzero exit): process dead, OR wedged (health beacon older than
5x the sweep interval), OR *stuck* flagged incidents — open longer than
OVERSEER_STUCK_MIN (default 12 min, just above the ~10-min recovery ladder) so a
freshly flagged incident mid-recovery does NOT wake the agent. The full `flagged`
set, `escalated_recent`, `needs_human`, the recent-errors tail, and socket
presence are report-only and never flip the exit code.

The health beacon is refreshed every sweep (a per-sweep heartbeat in
monitor_once), so its mtime is a true liveness signal: a hung sweep loop stops
touching it and correctly trips the wedged check, while a healthy watcher
refreshes it each cycle and never does.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import time

# Ensure local package imports work when this script is launched from an arbitrary
# directory or launchd sandbox (e.g. when run as a health probe).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from watcherdog.config import load_config
from watcherdog.incident_tracker import IncidentTracker

_SWEEP_RE = re.compile(r"Sweep:\s*(\d+)\s*chats,\s*(\d+)\s*healthy")
_TS_RE = re.compile(r"(\d{2}:\d{2})")
_ERR_RE = re.compile(r"Traceback|ERROR|CRITICAL")
_ESCALATED_WINDOW_S = 24 * 60 * 60   # surface escalations from the last 24h as context
# A flagged incident wakes the overseer only after it has been open longer than
# the core's auto-recovery ladder window (~10 min), so the probe never wakes
# during routine recovery. 12 min sits just above that ladder. Override via the
# OVERSEER_STUCK_MIN env var (read where build_report runs).
_STUCK_MIN_DEFAULT = 12              # minutes a flagged incident may stay open before it's "stuck"

# A live watcher's command line is "<python> [/path/]run_watcher.py …". Requiring
# the python interpreter excludes CARRIERS that merely mention the script — an
# editor (`vim run_watcher.py`), `tail -f …/run_watcher.py`, a shell history line —
# which a bare "run_watcher.py" substring would falsely report as "alive", masking
# a dead watcher. The `[ /]` makes the script the last path/word segment.
_PROC_PATTERN = r"[Pp]ython.*[ /]run_watcher\.py"


def _process_alive(pattern=_PROC_PATTERN):
    """True if a *python* process running run_watcher.py is alive. Uses `pgrep -f`
    with an interpreter-anchored pattern so non-python carriers don't false-positive;
    the probe's own argv is `overseer_health.py`, so it never matches itself. Any
    pgrep failure → False (fail-safe: the host then wakes Hermes)."""
    try:
        res = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=10)
        return res.returncode == 0 and bool(res.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _beacon_age_s(path, now):
    """Seconds since the health beacon was last touched, or None if absent."""
    try:
        return max(0.0, now - os.path.getmtime(path))
    except OSError:
        return None


def _flagged(db_path):
    """Open novel/cold-case incidents straight from SQLite (no socket needed).
    {"count","bots"} or {"count":0,"bots":[],"error":...} on any DB failure."""
    try:
        tr = IncidentTracker(db_path)
        try:
            rows = tr.novel_list()
        finally:
            tr.close()
        bots = [r["bot"] for r in rows]
        return {"count": len(bots), "bots": bots}
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "bots": [], "error": str(exc)}


def _stuck_min():
    """Minutes a flagged incident may stay open before the probe calls it *stuck*
    and trips the exit code. Module default ``_STUCK_MIN_DEFAULT`` (12), overridable
    by the ``OVERSEER_STUCK_MIN`` env var. A blank/unparseable env value falls back
    to the default (never crash the timer over a bad knob)."""
    raw = os.environ.get("OVERSEER_STUCK_MIN")
    if raw is None or not raw.strip():
        return _STUCK_MIN_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _STUCK_MIN_DEFAULT


def _flagged_stuck(db_path, now, stuck_min=_STUCK_MIN_DEFAULT):
    """Open novel incidents that have been open longer than ``stuck_min`` minutes —
    the subset that GATES the exit code (the core is no longer mid-ladder on them).

    Mirrors ``_flagged``/``_parked`` but applies the staleness threshold and carries
    each stuck bot's age. Shape:
    {"count":N, "bots":[...], "stale":[{"bot":..., "open_min":<int>}, ...]} and
    ``{"count":0,"bots":[],"stale":[],"error":...}`` on any DB failure.

    **Fail toward waking on unknown age.** If a flagged incident's ``opened_ts`` is
    absent / non-numeric / negative-age, the probe cannot prove the panel is still
    in-flight, so it is counted as stuck with ``"open_min": null`` and
    ``"reason": "age_unknown"`` — never silently treated as in-flight."""
    cutoff_s = stuck_min * 60.0
    try:
        tr = IncidentTracker(db_path)
        try:
            rows = tr.novel_list()
        finally:
            tr.close()
        bots, stale = [], []
        for r in rows:
            b = r["bot"]
            if not b or b in bots:
                continue          # one entry per panel (oldest opened wins via order)
            age_s = _coerce_age(r.get("opened_ts"), now)
            if age_s is None:
                # Unparseable / missing age → can't prove in-flight → stuck.
                bots.append(b)
                stale.append({"bot": b, "open_min": None, "reason": "age_unknown"})
            elif age_s > cutoff_s:
                bots.append(b)
                stale.append({"bot": b, "open_min": int(age_s // 60)})
            # else: younger than the threshold → core is laddering it → NOT stuck.
        return {"count": len(bots), "bots": bots, "stale": stale}
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "bots": [], "stale": [], "error": str(exc)}


def _coerce_age(opened_ts, now):
    """Seconds an incident has been open, or None if ``opened_ts`` is missing /
    non-numeric / yields a negative age (clock skew) — i.e. unprovable in-flight.
    SQLite stores ``opened_ts`` as REAL, but a corrupted/legacy row may carry a
    string or empty value; coerce defensively and never raise."""
    if opened_ts is None:
        return None
    try:
        age = now - float(opened_ts)
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def _escalated(db_path, since):
    """Recently-escalated incidents (auto-recovery gave up → human alerted, so they
    no longer appear in `flagged`/`list_flagged`). REPORT-ONLY context so an
    escalated-but-still-down panel isn't invisible behind `healthy: true` — the
    human was already alerted, so this never flips the exit code."""
    try:
        tr = IncidentTracker(db_path)
        try:
            rows = tr.escalated_list(since=since)
        finally:
            tr.close()
        # Unique panel names (a panel may have escalated more than once in the
        # window); count == len(bots), consistent with _flagged.
        bots = []
        for r in rows:
            b = r["bot"]
            if b and b not in bots:
                bots.append(b)
        return {"count": len(bots), "bots": bots}
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "bots": [], "error": str(exc)}


def _parked(db_path, now):
    """Panel cold-cases PARKED because the PC is OFF (human-owned: only a power-on
    fixes them). REPORT-ONLY context — like `escalated_recent` it must NEVER flip
    the exit code (a parked panel was already alerted once; re-waking Hermes on it
    would loop on a human-owned incident). Unlike escalated_recent there is NO
    fade: a still-off PC stays visible here forever. ``parked_h`` is hours since
    the park time (the row's ``resolved_ts``). Shape:
    {"count":N, "bots":[...], "stale":[{"bot":..., "parked_h":...}, ...]}."""
    try:
        tr = IncidentTracker(db_path)
        try:
            rows = tr.parked_list()
        finally:
            tr.close()
        bots, stale = [], []
        for r in rows:
            b = r["bot"]
            if not b or b in bots:
                continue          # one entry per panel (newest park wins via order)
            bots.append(b)
            parked_ts = r["resolved_ts"]
            parked_h = (round(max(0.0, now - parked_ts) / 3600.0, 1)
                        if parked_ts is not None else None)
            stale.append({"bot": b, "parked_h": parked_h})
        return {"count": len(bots), "bots": bots, "stale": stale}
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "bots": [], "stale": [], "error": str(exc)}


def _tail_lines(path, limit):
    """Last `limit` lines of a file (bounded), or [] if unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return list(collections.deque(fh, maxlen=limit))
    except OSError:
        return []


def _last_sweep(gui_log_path):
    """Newest 'Sweep: N chats, M healthy' line as 'HH:MM (N chats, M healthy)'."""
    for line in reversed(_tail_lines(gui_log_path, 400)):
        m = _SWEEP_RE.search(line)
        if m:
            ts = _TS_RE.search(line)
            stamp = (ts.group(1) + " ") if ts else ""
            return f"{stamp}({m.group(1)} chats, {m.group(2)} healthy)"
    return None


def _recent_errors(paths, limit=5):
    """Newest-first error/traceback lines across the logs, deduped + bounded."""
    collected = []
    for p in paths:
        for line in _tail_lines(p, 300):
            if _ERR_RE.search(line):
                collected.append(line.strip()[:300])
    seen, deduped = set(), []
    for line in reversed(collected):
        if line not in seen:
            seen.add(line)
            deduped.append(line)
        if len(deduped) >= limit:
            break
    return deduped


def build_report(cfg, now, *, alive_fn=_process_alive):
    """Gather health facts → (report_dict, exit_code). Pure given alive_fn.

    exit_code is 1 when a wake-trigger holds: process dead, wedged, or a *stuck*
    flagged incident (open longer than OVERSEER_STUCK_MIN — see flagged_stuck),
    else 0. The full `flagged` set, escalated_recent, needs_human, recent_errors,
    and socket_present are report-only and never flip the exit code.
    """
    data_dir = os.path.dirname(cfg.db_path) or "."
    alive = alive_fn()
    beacon_age = _beacon_age_s(getattr(cfg, "watcher_health_path", "") or "", now)
    stale_thr = 5 * float(getattr(cfg, "watch_poll_interval", 120) or 120)
    # A stale beacon means the sweep loop stopped heartbeating. Drive the wake
    # trigger off `beacon_stale` DIRECTLY (not gated on `alive`): if liveness is
    # ever misread, a stale beacon must still wake Hermes. `wedged` keeps its
    # "alive but not heartbeating" meaning for the human-readable field.
    beacon_stale = beacon_age is not None and beacon_age > stale_thr
    wedged = bool(alive and beacon_stale)
    flagged = _flagged(cfg.db_path)
    # The exit code gates on STUCK incidents (open past the recovery ladder), not
    # the full open set — so the probe doesn't wake the agent during routine
    # auto-recovery. `flagged` stays in the JSON unchanged for visibility.
    flagged_stuck = _flagged_stuck(cfg.db_path, now, _stuck_min())
    escalated = _escalated(cfg.db_path, now - _ESCALATED_WINDOW_S)
    needs_human = _parked(cfg.db_path, now)
    sock = getattr(cfg, "overseer_socket", "") or ""
    report = {
        "process_alive": alive,
        "beacon_age_s": None if beacon_age is None else round(beacon_age, 1),
        "wedged": wedged,
        "flagged": flagged,
        "flagged_stuck": flagged_stuck,
        "escalated_recent": escalated,
        "needs_human": needs_human,
        "last_sweep": _last_sweep(getattr(cfg, "gui_run_log", "") or ""),
        "recent_errors": _recent_errors(
            [os.path.join(data_dir, "telegram.err.log"),
             getattr(cfg, "gui_run_log", "") or ""]),
        "socket_present": (os.path.exists(sock) if sock else None),
    }
    # NOTE: escalated_recent AND needs_human are REPORT-ONLY — auto-recovery already
    # alerted the human, so neither flips the exit code (else Hermes would be woken
    # in a loop on a human-owned incident). escalated_recent fades at 24h; needs_human
    # (parked / PC-off) never fades so a still-off PC stays visible — but still must
    # NOT wake Hermes. They only restore VISIBILITY of a down-but-human-owned panel.
    # The flagged gate uses flagged_stuck (not the full `flagged` set): a freshly
    # flagged incident the core is laddering must NOT wake the agent — only one that
    # has outlived the recovery window (or whose age is unprovable) does.
    unhealthy = (not alive) or beacon_stale or flagged_stuck["count"] > 0
    report["healthy"] = not unhealthy
    return report, (1 if unhealthy else 0)


def main(argv=None):
    cfg = load_config()
    report, code = build_report(cfg, time.time())
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
