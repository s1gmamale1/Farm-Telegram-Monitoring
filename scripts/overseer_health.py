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
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import time

from watcherdog.config import load_config
from watcherdog.incident_tracker import IncidentTracker

_SWEEP_RE = re.compile(r"Sweep:\s*(\d+)\s*chats,\s*(\d+)\s*healthy")
_TS_RE = re.compile(r"(\d{2}:\d{2})")
_ERR_RE = re.compile(r"Traceback|ERROR|CRITICAL")


def _process_alive(pattern="run_watcher.py"):
    """True if a process whose command line contains `pattern` is running. Uses
    `pgrep -f`; the probe's own argv is `overseer_health.py`, so it never matches
    itself. Any pgrep failure → False (fail-safe: the host then wakes Hermes)."""
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
