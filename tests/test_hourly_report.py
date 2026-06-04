"""Tests for the hourly farm report's PC-map handling."""

from __future__ import annotations

import types

from watcherdog import mcp_watcher, roster


def test_invert_pc_to_bots_mapping():
    # {PC: [bots]} — the natural way to write it — inverts to bot -> PC.
    raw = {"1": [1, 2], "5": [23, 24], "6": [7, 8]}
    got = roster._invert_pc_map(raw)
    assert got == {1: "1", 2: "1", 23: "5", 24: "5", 7: "6", 8: "6"}


def test_accepts_already_bot_keyed_mapping():
    # {bot: PC} is passed through unchanged.
    raw = {"3": "2", "4": "2"}
    assert roster._invert_pc_map(raw) == {3: "2", 4: "2"}


def test_hourly_dedupe_one_per_clock_hour(tmp_path):
    cfg = types.SimpleNamespace(db_path=str(tmp_path / "incidents.db"))
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-03 10") is False
    mcp_watcher._hourly_mark_sent(cfg, "2026-06-03 10")
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-03 10") is True   # same hour → skip
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-03 11") is False  # next hour → send


def test_full_24_bot_map_covers_every_bot():
    raw = {"1": [1, 2], "2": [3, 4], "3": [5, 6], "4": [9, 10], "5": [23, 24],
           "6": [7, 8], "7": [11, 12], "8": [15, 16], "9": [13, 14],
           "10": [17, 18], "11": [19, 20], "12": [21, 22]}
    got = roster._invert_pc_map(raw)
    assert len(got) == 24                       # every bot mapped
    assert all(b in got for b in range(1, 25))  # 1..24 all present
    assert len(set(got.values())) == 12         # across 12 PCs
