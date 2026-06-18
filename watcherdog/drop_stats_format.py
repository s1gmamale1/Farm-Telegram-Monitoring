"""Pure (Telegram-free) helpers for the Drop Stats job: week ids, buffer file
I/O, report-row parsing (via farm_stats), and the ibo report renderer. Split out
of drop_stats.py so that module stays focused on panel-driving + scheduling and
under the 500-line limit. Re-exported from drop_stats for backward compatibility.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from watcherdog import farm_stats

log = logging.getLogger("watcherdog.drop_stats_format")


# --- pure helpers (unit-tested) --------------------------------------------
def iso_week(dt):
    """Canonical week id like ``2026-W23`` (ISO year + zero-padded ISO week).

    Used both for the buffer filename and ibo's report header so they match.
    """
    iso = dt.isocalendar()
    year, week = iso[0], iso[1]
    return f"{year}-W{week:02d}"


def buffer_path(drop_stats_dir, week):
    """Path of the weekly buffer file for ``week`` (e.g. .../2026-W23.json)."""
    return os.path.join(drop_stats_dir, f"{week}.json")


def panel_label(name):
    """Display name -> ``Panel#N`` using the number in the name (skill 0)."""
    m = re.search(r"(\d+)", name or "")
    return f"Panel#{m.group(1)}" if m else (name.strip() if name else "Panel#?")


def _report_to_row(text):
    """Parse a panel's Drop Stats reply into the sheet-column fields, via the
    Phase 1 farm_stats parser. Empty/echo text -> all-blank (so format_report and
    the Sheets push show nothing rather than a fabricated 0). Never raises."""
    rep = farm_stats.parse_drop_report(text)
    has = (rep.total_cases is not None or rep.value_usd is not None
           or rep.accounts is not None)
    if not has and not rep.problems:
        return {"drops": "", "items": "", "value": "", "notes": ""}
    return {
        "drops": rep.total_cases if rep.total_cases is not None else "",
        "items": len(rep.skins),
        "value": rep.value_usd if rep.value_usd is not None else "",
        "notes": "; ".join(rep.problems),
    }


def make_row(week, panel, parsed, *, date=None):
    """Build one sheet row (keyed by drop_sheets.COLUMNS) for a panel."""
    parsed = parsed or {}
    return {
        "week": week,
        "date": date or datetime.now().date().isoformat(),
        "panel": panel,
        "drops": parsed.get("drops", ""),
        "items": parsed.get("items", ""),
        "value": parsed.get("value", ""),
        "notes": parsed.get("notes", ""),
    }


def write_buffer(path, week, rows, *, generated=None):
    """Write the weekly buffer (one file per week, overwriting). Returns payload.

    Never raises — a failed write is logged so the caller can still push/report.
    """
    payload = {
        "week": week,
        "generated": generated or datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("could not write drop-stats buffer %s: %s", path, exc)
    return payload


def load_buffer(path):
    """Read a weekly buffer back (or ``None`` if missing/unreadable)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read drop-stats buffer %s: %s", path, exc)
        return None


def format_report(week, rows, push=None):
    """Render ibo's weekly report (skill 5 step 5).

        🐕 Weekly drops — 2026-W23
        • Panel#1 — 312 drops · ~$xx
        • Panel#2 — 280 drops
        Total: 592 · saved to Sheets ✅
    """
    lines = [f"🐕 Weekly drops — {week}"]
    total = 0
    for r in rows:
        drops = str(r.get("drops", "") or "")
        value = str(r.get("value", "") or "")
        try:
            total += int(drops)
        except (TypeError, ValueError):
            pass
        shown = drops or "?"
        val = f" · ~${value}" if value else ""
        lines.append(f"• {r.get('panel', 'Panel#?')} — {shown} drops{val}")

    push = push or {}
    if push.get("ok"):
        tail = f"Total: {total} · saved to Sheets ✅"
    elif push.get("reason") == "not configured":
        tail = f"Total: {total} · buffered, no API key yet"
    else:
        reason = push.get("reason", "")
        tail = f"Total: {total} · buffered ({reason})" if reason else f"Total: {total} · buffered"
    lines.append(tail)
    return "\n".join(lines)
