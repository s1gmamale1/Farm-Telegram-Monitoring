"""Tests for watcherdog.fast_commands — deterministic slash-commands (no LLM)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from watcherdog import commands, daily_report, fast_commands, roster


def _cfg(**kw):
    base = dict(quiet_threshold_minutes=60, silence_threshold=1800,
                agent_actions_enabled=True, bot_actions_enabled=True,
                bot_self_edit_enabled=True, bot_self_restart_enabled=True,
                daily_errors_path="/tmp/wd_fast_none.jsonl")
    base.update(kw)
    return SimpleNamespace(**base)


_STATUSES = {
    1: {"pc": "1", "status": roster.FARMING, "age_min": 3, "name": "SF1"},
    3: {"pc": "2", "status": roster.ATTENTION, "age_min": 95, "name": "SF3"},
    9: {"pc": "5", "status": roster.DEAD, "age_min": 510, "name": "SF9"},
}


# --- formatting -------------------------------------------------------------

def test_status_overview_counts_and_lists():
    out = fast_commands._status(_STATUSES)
    assert "1/3 farming" in out
    assert "SF9" in out and "SF3" in out


def test_status_all_healthy():
    healthy = {1: {"pc": "1", "status": roster.FARMING, "age_min": 2, "name": "SF1"}}
    assert "everything farming" in fast_commands._status(healthy)


def test_problems_lists_only_bad():
    out = fast_commands._problems(_STATUSES)
    assert "SF3" in out and "SF9" in out and "SF1" not in out


def test_problems_none():
    healthy = {1: {"pc": "1", "status": roster.FARMING, "age_min": 2, "name": "SF1"}}
    assert "No problems" in fast_commands._problems(healthy)


def test_silent_uses_threshold():
    # silence_threshold 1800s = 30m; SF3 (95m) and SF9 (510m) are silent, SF1 (3m) not.
    out = fast_commands._silent(_STATUSES, _cfg())
    assert "SF3" in out and "SF9" in out and "SF1" not in out


def test_mode_reflects_flags():
    assert "DRY-RUN" in fast_commands._mode(_cfg(), False)
    assert "READ-ONLY" in fast_commands._mode(_cfg(agent_actions_enabled=False), True)
    assert "LIVE" in fast_commands._mode(_cfg(), True)


def test_fixes_reads_log(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="SF3", error="proxy", fix="relaunch",
                        ts="2026-06-04T12:00:00")
    out = fast_commands._fixes(_cfg(daily_errors_path=str(p)))
    assert "SF3" in out
    assert "No fixes" in fast_commands._fixes(_cfg(daily_errors_path=str(tmp_path / "x.jsonl")))


# --- dispatch + parsing -----------------------------------------------------

def test_handle_dispatches_without_scan_for_mode():
    # /mode and /fixes must not require a client/watch (no roster scan).
    out = asyncio.run(fast_commands.handle("mode", "", cfg=_cfg(), client=None,
                                           watch=None, deliver=True))
    assert "Mode:" in out


def test_handle_scans_for_problems(monkeypatch):
    async def fake_scan(client, cfg, watch):
        return _STATUSES
    monkeypatch.setattr(fast_commands.roster, "scan", fake_scan)
    out = asyncio.run(fast_commands.handle("problems", "", cfg=_cfg(),
                                           client=None, watch=[], deliver=True))
    assert "SF9" in out


def test_fast_parse_recognizes_and_aliases():
    assert commands.fast_parse("/status")[0] == "status"
    assert commands.fast_parse("/down")[0] == "problems"
    assert commands.fast_parse("/health")[0] == "status"
    assert commands.fast_parse("/weekly") is None   # still an AI command
    assert commands.fast_parse("not a command") is None


def test_fast_commands_not_in_ai_map():
    # The deterministic commands must never expand to an agent prompt.
    for name in ("status", "problems", "silent", "fixes", "mode"):
        assert name not in commands.COMMANDS
