"""WatcherDog's daily AI-fix log + report (skill 2).

Every error Hermes fixes *by itself* is appended as one JSON line to
``data/hermes/daily_errors.jsonl``. The log is reported to ibo:

  * at **end of day** (a scheduled summary), and
  * **immediately on startup** if it's non-empty — that means the PC/app went
    down before the day's report was sent, so we flush it right away.

After a successful report the file is **emptied** (:func:`clear_log`).

See docs/hermes/skills/02-error-handling.md. Pure stdlib.

Typical use::

    daily_report.record(cfg.daily_errors_path,
                        panel="Panel#3", error="proxy timeout",
                        fix="restarted panel", result="ok")
    ...
    text = daily_report.build_report(cfg.daily_errors_path)   # None if empty
    if text:
        send_to_ibo(text)
        daily_report.clear_log(cfg.daily_errors_path)         # after a good send
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("watcherdog.daily_report")


def record(path, *, panel, error, fix, result="ok", ts=None):
    """Append one AI-fix entry as a JSON line. Returns the entry dict.

    ``ts`` defaults to now (ISO seconds). Never raises — a failed write is
    logged and the entry returned so the caller can still report it.
    """
    entry = {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "panel": panel,
        "error": error,
        "fix": fix,
        "result": result,
    }
    if not path:
        # No log path configured: honor the "never raises" contract (a None
        # path used to TypeError inside os.path.dirname) and still return the
        # entry so the caller can report it.
        return entry
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("could not record daily error to %s: %s", path, exc)
    return entry


def load_entries(path):
    """Read all entries from the log. Skips blank/malformed lines. Missing -> []."""
    if not path or not os.path.exists(path):
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("skipping malformed daily-error line: %.80s", line)
    except OSError as exc:
        logger.warning("could not read daily errors %s: %s", path, exc)
    return entries


def has_pending(path):
    """True if the log has at least one valid entry to report."""
    return bool(load_entries(path))


def format_report(entries, *, cleared=True):
    """Render entries into ibo's end-of-day report. Returns None if empty.

    Entries are grouped by (panel, error, fix, result) with an ``×N`` count,
    preserving first-seen order::

        🐕 Today — 3 errors auto-fixed
        • Panel#1 ×2 — proxy timeout → restarted, ok
        • Panel#3 ×1 — Steam stuck → killed & relaunched, ok
        (file cleared)
    """
    if not entries:
        return None
    groups, order = {}, []
    for e in entries:
        key = (e.get("panel", "?"), e.get("error", "?"),
               e.get("fix", "?"), e.get("result", "ok"))
        if key not in groups:
            groups[key] = 0
            order.append(key)
        groups[key] += 1

    total = len(entries)
    lines = [f"🐕 Today — {total} error{'s' if total != 1 else ''} auto-fixed"]
    for panel, error, fix, result in order:
        lines.append(f"• {panel} ×{groups[(panel, error, fix, result)]} — "
                     f"{error} → {fix}, {result}")
    if cleared:
        lines.append("(file cleared)")
    return "\n".join(lines)


def build_report(path, *, cleared=True):
    """Convenience: load + format the log at ``path``. None if nothing pending."""
    return format_report(load_entries(path), cleared=cleared)


def entries_since(path, since_iso):
    """Entries whose ``ts`` is at or after ``since_iso``.

    Timestamps are written by :func:`record` via ``datetime.isoformat`` (same
    fixed format, no timezone), so a plain string compare orders them correctly.
    """
    since = since_iso or ""
    return [e for e in load_entries(path) if (e.get("ts") or "") >= since]


def summary_since(path, since_iso, *, max_items=12):
    """A compact one-liner of fixes applied since ``since_iso`` — for the hourly
    report. Groups by (panel, error, result) with ``×N`` counts. None if no
    fixes in the window::

        🔧 Fixed last hour: SF3 proxy timeout; SF7 CS frozen ×2; SF9 relaunch ⚠️
    """
    entries = entries_since(path, since_iso)
    if not entries:
        return None
    groups, order = {}, []
    for e in entries:
        key = (e.get("panel", "?"), e.get("error", "?"), e.get("result", "ok"))
        if key not in groups:
            groups[key] = 0
            order.append(key)
        groups[key] += 1
    parts = []
    for panel, error, result in order[:max_items]:
        n = groups[(panel, error, result)]
        count = f" ×{n}" if n > 1 else ""
        flag = "" if result == "ok" else " ⚠️"
        parts.append(f"{panel} {error}{count}{flag}")
    line = "🔧 Fixed last hour: " + "; ".join(parts)
    if len(order) > max_items:
        line += f"; +{len(order) - max_items} more"
    return line


def clear_log(path):
    """Empty the log (truncate to zero bytes). Call after a successful report."""
    if not path or not os.path.exists(path):
        return
    try:
        open(path, "w", encoding="utf-8").close()
    except OSError as exc:
        logger.warning("could not clear daily errors %s: %s", path, exc)
