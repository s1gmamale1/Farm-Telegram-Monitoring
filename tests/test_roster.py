"""Tests for watcherdog.roster — deterministic bot classification (no LLM)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from watcherdog import roster


def _cfg(quiet=60):
    return SimpleNamespace(quiet_threshold_minutes=quiet)


def test_extract_account_count():
    assert roster.extract_account_count("accounts: 4 launched") == 4
    assert roster.extract_account_count("accounts=2") == 2
    assert roster.extract_account_count("no number here") is None


def test_farming_indicator():
    assert roster.farming_indicator("warmup started") is True
    assert roster.farming_indicator("match found, lobby ready") is True
    assert roster.farming_indicator("collected drop") is False


def test_invert_pc_map_both_shapes():
    assert roster._invert_pc_map({"5": [9, 10]}) == {9: "5", 10: "5"}
    assert roster._invert_pc_map({"9": "5"}) == {9: "5"}


def test_classify_status_dead_when_very_old():
    assert roster.classify_status("anything", 200, _cfg()) == roster.DEAD


def test_classify_status_attention_on_error():
    assert roster.classify_status("ERROR: proxy timeout", 5, _cfg()) == roster.ATTENTION


def test_classify_status_attention_wrong_account_count():
    # Routine-looking but only 2 accounts up -> needs attention.
    assert roster.classify_status("accounts: 2 warmup started", 5, _cfg()) == roster.ATTENTION


def test_classify_status_farming_when_recent_and_active():
    assert roster.classify_status("warmup started, match soon", 5, _cfg()) == roster.FARMING


def test_classify_status_quiet_when_no_activity():
    assert roster.classify_status("collected drop", 5, _cfg()) == roster.QUIET


def test_scan_reads_and_classifies(monkeypatch):
    async def fake_latest(client, ent, mark_read=False):
        return {"SF1": ("warmup started", _Dated(5)),
                "SF9": ("ERROR: crashed", _Dated(5))}[ent]

    monkeypatch.setattr(roster.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(roster, "load_pc_map", lambda cfg: {1: "1", 9: "5"})
    watch = [("SF1", "SF1"), ("SF9", "SF9")]
    out = asyncio.run(roster.scan(None, _cfg(), watch))
    assert out[1]["status"] == roster.FARMING
    assert out[9]["status"] == roster.ATTENTION
    assert out[9]["pc"] == "5"


class _Dated:
    """Stand-in for a Telethon message date — N minutes ago."""
    def __init__(self, age_min):
        import time
        self._ts = time.time() - age_min * 60

    def timestamp(self):
        return self._ts
