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


_SEV_RANK = {"critical": 0, "high": 1, "low": 3}


def _red_rank(num, info, inc):
    """Sort key for 🔴 panels: severity asc (critical first), then oldest first."""
    sev = (inc or {}).get("severity")
    return (_SEV_RANK.get(sev, 2), -info["age_min"])


def _red_line(num, info, is_new, action):
    new = " 🆕" if is_new else ""
    parts = [_panel_reason(info), _fmt_age(info["age_min"])]
    if action:
        parts.append(action)
    return f"🔴 SF{num}{new} — " + " · ".join(parts)


def _amber_token(num, info, is_new):
    tok = f"SF{num} {_fmt_age(info['age_min'])}"
    return f"{tok} 🆕" if is_new else tok


def build(fleet, incidents, fix_line, prev_state, now):
    """Render the hourly report. Returns ``(text, new_state)``. Pure and total —
    never raises on empty/odd inputs."""
    items = sorted(fleet.items())
    cur_snapshot = _snapshot(fleet)
    prev_snapshot = (prev_state or {}).get("last_snapshot") or {}
    new_flagged, recovered = _diff(prev_snapshot, cur_snapshot)
    if not prev_snapshot:
        new_flagged = set()  # first run / no baseline → nothing is "new" yet
    inc_idx = _index_incidents(incidents)

    new_state = {
        "last_hour": now.strftime("%Y-%m-%d %H"),
        "last_sent_iso": now.isoformat(timespec="seconds"),
        "last_snapshot": cur_snapshot,
    }
    hhmm = now.strftime("%H:%M")
    total = len(items)

    if total == 0:
        return f"🐕 {hhmm} — no panels in watch", new_state

    farming = [(n, i) for n, i in items if i["status"] == roster.FARMING]
    quiet = [(n, i) for n, i in items if i["status"] == roster.QUIET]
    attn = [(n, i) for n, i in items if i["status"] == roster.ATTENTION]
    dead = [(n, i) for n, i in items if i["status"] == roster.DEAD]

    # All-green fast path — keep the channel quiet when nothing is wrong.
    if len(farming) == total:
        tail = fix_line or "🔧 no fixes needed"
        return f"🐕 {hhmm} — ✅ all {total} farming · {tail}", new_state

    lines = []
    header = f"🐕 Hourly Report — {hhmm}"
    gap = _gap_line(prev_state, now)
    if gap:
        header += f"          {gap}"
    lines.append(header)
    lines.append(
        f"✅ {len(farming)} farming · ⚠️ {len(quiet)} quiet · "
        f"🔴 {len(attn)} attention · 💀 {len(dead)} dead   ({total} panels)")
    lines.append("")

    if dead or attn or quiet:
        lines.append("NEEDS ATTENTION")
        for n, info in sorted(dead):
            lines.append(f"💀 SF{n} — silent {_fmt_age(info['age_min'])}")
        for n, info in sorted(
                attn, key=lambda ni: _red_rank(ni[0], ni[1], inc_idx.get(ni[1]["name"]))):
            action = _panel_action(inc_idx.get(info["name"]))
            lines.append(_red_line(n, info, str(n) in new_flagged, action))
        if quiet:
            toks = [_amber_token(n, info, str(n) in new_flagged)
                    for n, info in sorted(quiet, key=lambda ni: -ni[1]["age_min"])]
            lines.append("⚠️ " + " · ".join(toks))
        lines.append("")

    if farming:
        names = " ".join(f"SF{n}" for n, _ in farming)
        lines.append(f"✅ FARMING ({len(farming)}): {names}")
    if recovered:
        since = _prev_hhmm(prev_state)
        rec = " ".join(f"SF{n}" for n in recovered)
        lines.append(
            f"✅ recovered since {since}: {rec}" if since else f"✅ recovered: {rec}")

    lines.append("")
    lines.append(fix_line or "🔧 No fixes needed last hour.")
    return "\n".join(lines).rstrip(), new_state
