"""Tests for watcherdog.fleet_report — deterministic report commands (Phase 2)."""

from __future__ import annotations

import asyncio
import json
import os

from watcherdog import drop_stats, fleet_report
from watcherdog.farm_stats import BotStats


def _write_buffer(d, week, rows, generated="2026-06-10T00:00:05"):
    os.makedirs(d, exist_ok=True)
    path = drop_stats.buffer_path(d, week)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"week": week, "generated": generated, "rows": rows}, fh)
    return path


def test_load_latest_buffer_keys_rows_by_bot_number(tmp_path):
    _write_buffer(str(tmp_path), "2026-W24", [
        {"panel": "Panel#3", "drops": 28, "value": 31.5, "items": 2},
        {"panel": "Panel#7", "drops": 10, "value": 4.0, "items": 0},
    ])
    by_num, week, collected = fleet_report._load_latest_buffer(str(tmp_path))
    assert week == "2026-W24"
    assert collected == "2026-06-10"
    assert by_num[3]["value"] == 31.5
    assert by_num[7]["drops"] == 10


def test_load_latest_buffer_missing_dir_is_empty():
    by_num, week, collected = fleet_report._load_latest_buffer("/no/such/dir")
    assert by_num == {} and week is None and collected is None


def test_load_latest_buffer_falls_back_to_newest_week(tmp_path):
    _write_buffer(str(tmp_path), "2026-W22", [{"panel": "Panel#1", "drops": 1, "value": 1.0}])
    _write_buffer(str(tmp_path), "2026-W24", [{"panel": "Panel#1", "drops": 9, "value": 9.0}])
    by_num, week, _ = fleet_report._load_latest_buffer(str(tmp_path))
    assert week == "2026-W24" and by_num[1]["drops"] == 9
