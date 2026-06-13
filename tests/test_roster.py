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


# --- status_emoji -----------------------------------------------------------

def test_status_emoji_all_buckets():
    assert roster.status_emoji(roster.FARMING) == "✅"
    assert roster.status_emoji(roster.QUIET) == "⚠️"
    assert roster.status_emoji(roster.ATTENTION) == "🔴"
    assert roster.status_emoji(roster.DEAD) == "💀"


def test_status_emoji_unknown_returns_question():
    assert roster.status_emoji("some unknown bucket") == "❓"


# --- classify_status: age boundaries ----------------------------------------

def test_classify_status_attention_at_91_min():
    """91 min with no error but age > 90 should be ATTENTION, not QUIET."""
    assert roster.classify_status("collected drop", 91, _cfg(quiet=60)) == roster.ATTENTION


def test_classify_status_quiet_at_exactly_90_min():
    """At exactly 90 min the age heuristic doesn't fire (> 90, not >= 90)."""
    assert roster.classify_status("collected drop", 90, _cfg(quiet=60)) == roster.QUIET


def test_classify_status_attention_when_no_text_and_recent():
    """No text (bot never replied) → not farming → ATTENTION."""
    assert roster.classify_status(None, 5, _cfg()) == roster.ATTENTION


# --- load_pc_map: file-based ------------------------------------------------

def test_load_pc_map_reads_file(tmp_path, monkeypatch):
    import json

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "farmer_pc_map.json").write_text(
        json.dumps({"PC1": [1, 2], "PC2": [3]}), encoding="utf-8"
    )
    # Make __file__-based path resolution point into tmp_path.
    # roster.py does: base = dirname(dirname(abspath(__file__)))
    # If abspath returns tmp_path/watcherdog/roster.py, base becomes tmp_path.
    monkeypatch.setattr(roster.os.path, "abspath",
                        lambda _: str(tmp_path / "watcherdog" / "roster.py"))
    roster._pc_map_cache = None
    mapping = roster.load_pc_map(_cfg())
    roster._pc_map_cache = None
    assert mapping.get(1) == "PC1"
    assert mapping.get(3) == "PC2"


def test_load_pc_map_missing_file_returns_empty(tmp_path, monkeypatch):
    """When farmer_pc_map.json doesn't exist, load_pc_map returns {} gracefully."""
    # tmp_path/data/ exists but has no farmer_pc_map.json.
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(roster.os.path, "abspath",
                        lambda _: str(tmp_path / "watcherdog" / "roster.py"))
    roster._pc_map_cache = None
    mapping = roster.load_pc_map(_cfg())
    roster._pc_map_cache = None
    assert mapping == {}


def test_classify_status_detailed_error_returns_reason():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    status, code, detail = roster.classify_status_detailed(
        "❌ proxy timeout — connection refused", 5.0, _Cfg())
    assert status == roster.ATTENTION
    assert code == "error"
    assert detail  # a non-empty human summary
    assert len(detail) <= 160


def test_classify_status_detailed_accounts_mismatch():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    # "accounts: 2 warmup started" — classify() returns "normal" (matches warmup
    # started in _NORMAL_RE, no error tokens), but account count is 2 != 4.
    status, code, detail = roster.classify_status_detailed(
        "accounts: 2 warmup started", 3.0, _Cfg())
    assert status == roster.ATTENTION
    assert code == "accounts"
    assert detail == "accounts 2/4"


def test_classify_status_detailed_farming_and_quiet():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    # "warmup started" is classified "normal" by classify() and triggers farming_indicator.
    s_farm, c_farm, _ = roster.classify_status_detailed(
        "warmup started", 2.0, _Cfg())
    assert s_farm == roster.FARMING and c_farm == ""

    # "idle" is classified "unknown" by classify() but has no error tokens; here
    # we use "collected drop" which classify() maps to "normal" with no farming signal.
    s_quiet, c_quiet, _ = roster.classify_status_detailed(
        "collected drop", 5.0, _Cfg())
    assert s_quiet == roster.QUIET and c_quiet == "quiet"


def test_classify_status_backcompat_returns_status_only():
    from watcherdog import roster

    class _Cfg:
        quiet_threshold_minutes = 60

    assert roster.classify_status("❌ error", 5.0, _Cfg()) == roster.ATTENTION
    # "warmup started" is classified "normal" by classify() and triggers farming_indicator.
    assert roster.classify_status("warmup started", 2.0, _Cfg()) == roster.FARMING


def test_full_24_bot_map_covers_every_bot():
    raw = {"1": [1, 2], "2": [3, 4], "3": [5, 6], "4": [9, 10], "5": [23, 24],
           "6": [7, 8], "7": [11, 12], "8": [15, 16], "9": [13, 14],
           "10": [17, 18], "11": [19, 20], "12": [21, 22]}
    got = roster._invert_pc_map(raw)
    assert len(got) == 24
    assert all(b in got for b in range(1, 25))
    assert len(set(got.values())) == 12
