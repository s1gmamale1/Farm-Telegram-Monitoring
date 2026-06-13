"""Deterministic hourly farm report — pure builder, NO LLM, NO Telethon.

Mirrors ``fleet_report.py``: every formatting and diff decision is a pure
function over plain data, so the whole report is unit-testable with dicts. The
orchestrator (``mcp_watcher.run_hourly_report``) gathers the inputs, calls
``build()``, sends the text, and persists the returned state.

Inputs to ``build()``:
  * ``fleet``     — ``roster.scan`` result: ``{bot_num: {status, age_min, name,
                    reason_code, reason_detail, pc}}``
  * ``incidents`` — ``IncidentTracker.open_list()`` (list of dicts; joined by
                    ``inc["bot"] == info["name"]``)
  * ``fix_line``  — ``daily_report.summary_since(...)`` result (str or None)
  * ``prev_state``— the previous ``hourly_report_state.json`` dict (or {})
  * ``now``       — a ``datetime``

Returns ``(text, new_state)``. ``new_state`` is persisted by the caller ONLY
after a successful send, so a failed send never poisons the next diff/gap.
"""
from __future__ import annotations

from datetime import datetime

from watcherdog import roster

_FLAGGED_EMOJI = ("🔴", "⚠️", "💀")


def _snapshot(fleet):
    """Compact ``{str(bot_num): status_emoji}`` for the diff."""
    return {str(n): roster.status_emoji(info["status"]) for n, info in fleet.items()}


def _diff(prev_snapshot, cur_snapshot):
    """``(new_flagged:set[str], recovered:list[int])`` between two snapshots.

    new_flagged: now ``🔴/⚠️/💀`` but previously ``✅`` or absent.
    recovered:   previously ``🔴/⚠️/💀`` and now ``✅``.
    """
    new_flagged = set()
    for num_str, emoji in cur_snapshot.items():
        prev = prev_snapshot.get(num_str)
        if emoji in _FLAGGED_EMOJI and prev in (None, "✅"):
            new_flagged.add(num_str)
    recovered = [
        int(num_str)
        for num_str, prev in prev_snapshot.items()
        if prev in _FLAGGED_EMOJI and cur_snapshot.get(num_str) == "✅"
    ]
    recovered.sort()
    return new_flagged, recovered


def _gap_line(prev_state, now):
    """``⏰ gap`` line when the last send was >70 min ago, else None."""
    iso = (prev_state or {}).get("last_sent_iso")
    if not iso:
        return None
    try:
        prev = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    delta_min = (now - prev).total_seconds() / 60.0
    if delta_min <= 70:
        return None
    ago = f"{delta_min / 60.0:.0f}h" if delta_min >= 60 else f"{delta_min:.0f}m"
    return f"⏰ gap: last report {prev.strftime('%H:%M')} ({ago} ago)"


def _prev_hhmm(prev_state):
    """``HH:MM`` of the previous send, or None."""
    iso = (prev_state or {}).get("last_sent_iso")
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def _truncate(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _fmt_age(age_min):
    """``24m`` for a real age; ``?`` for the sentinel (no message ever seen)."""
    return "?" if age_min >= 10000 else f"{age_min:.0f}m"


def _index_incidents(incidents):
    """``{bot_name: incident_dict}`` keeping the most-recent row per bot.

    ``open_list()`` is ordered by ``opened_ts`` ascending, so a later row in the
    list is newer — assigning unconditionally lets it win.
    """
    idx = {}
    for inc in incidents or []:
        bot = inc.get("bot")
        if bot:
            idx[bot] = inc
    return idx


def _panel_action(inc):
    """What the watcher has done about a flagged panel, from its open incident.

    Empty string when there's no incident to report. Truthful: only renders what
    the DB actually records — never the monitor loop's in-memory 'armed' state.
    """
    if inc is None:
        return ""
    if inc.get("novel"):
        return "cold-cased, needs PC"
    fix = (inc.get("fix_attempted") or "").strip()
    if not fix:
        return "incident open"
    label = fix.replace("_", " ").replace("-", " ").strip()
    retries = inc.get("fix_retries") or 0
    return f"{label} ×{retries + 1}" if retries else label


_REASON_LABELS = {"stale": "stale", "quiet": "quiet", "dead": "silent"}


def _panel_reason(info):
    """Short 'why flagged' string — the detail when present, else a code label."""
    detail = (info.get("reason_detail") or "").strip()
    if detail:
        return _truncate(detail, 60)
    code = info.get("reason_code") or ""
    return _REASON_LABELS.get(code, code or "flagged")
