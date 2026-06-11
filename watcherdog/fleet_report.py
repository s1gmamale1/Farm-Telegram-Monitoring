"""Deterministic report commands — answered with NO model (Phase 2).

/weekly /value /top /worst /check /compare /bans /today read one cheap
latest_message sweep (mirroring roster.scan, no button presses) merged with the
latest weekly drop buffer (the only place per-panel $ values are collected). Pure
formatters render phone-sized replies. No LLM — instant and free.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from watcherdog import drop_stats, farm_stats, roster, tg_tools
from watcherdog.classifier import severity_of, summarize
from watcherdog.farm_stats import BotStats

logger = logging.getLogger("watcherdog.fleet_report")

_NUM_RE = re.compile(r"(\d+)")


@dataclass
class FleetEntry:
    num: int
    name: str
    pc: str = "?"
    status: str = ""
    age_min: float = 0.0
    last_text: str = ""
    stats: BotStats = field(default_factory=BotStats)


@dataclass
class Fleet:
    entries: list = field(default_factory=list)   # list[FleetEntry], sorted by num
    week: str | None = None
    collected: str | None = None                  # short date label, e.g. "2026-06-10"


def _short_date(generated):
    """'2026-06-10T00:00:05' -> '2026-06-10'; None/garbage -> None."""
    if not generated or not isinstance(generated, str):
        return None
    return generated.split("T", 1)[0]


def _newest_buffer(d):
    """Load the lexicographically-latest <YYYY-Www>.json in d (ISO weeks sort
    correctly), or None."""
    try:
        files = [f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return None
    if not files:
        return None
    return drop_stats.load_buffer(os.path.join(d, max(files)))


def _load_latest_buffer(drop_stats_dir):
    """Return (rows_by_bot_number, week, collected_label).

    Tries the current ISO week, then falls back to the newest buffer file. Empty
    ({}, None, None) when nothing is readable. Never raises."""
    if not drop_stats_dir:
        return {}, None, None
    payload = drop_stats.load_buffer(
        drop_stats.buffer_path(drop_stats_dir, drop_stats.iso_week(datetime.now())))
    if payload is None:
        payload = _newest_buffer(drop_stats_dir)
    if not payload:
        return {}, None, None
    by_num = {}
    for r in (payload.get("rows") or []):
        m = _NUM_RE.search(str(r.get("panel", "")))
        if m:
            by_num[int(m.group(1))] = r
    return by_num, payload.get("week"), _short_date(payload.get("generated"))
