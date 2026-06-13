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
5x the sweep interval), OR open flagged incidents in the DB. The recent-errors
tail and socket presence are report-only and never flip the exit code.

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

    exit_code is 1 when a wake-trigger holds (process dead, wedged, or any
    flagged incident), else 0. recent_errors + socket_present are report-only.
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
    sock = getattr(cfg, "overseer_socket", "") or ""
    report = {
        "process_alive": alive,
        "beacon_age_s": None if beacon_age is None else round(beacon_age, 1),
        "wedged": wedged,
        "flagged": flagged,
        "last_sweep": _last_sweep(getattr(cfg, "gui_run_log", "") or ""),
        "recent_errors": _recent_errors(
            [os.path.join(data_dir, "telegram.err.log"),
             getattr(cfg, "gui_run_log", "") or ""]),
        "socket_present": (os.path.exists(sock) if sock else None),
    }
    unhealthy = (not alive) or beacon_stale or flagged.get("count", 0) > 0
    report["healthy"] = not unhealthy
    return report, (1 if unhealthy else 0)


def main(argv=None):
    cfg = load_config()
    report, code = build_report(cfg, time.time())
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
