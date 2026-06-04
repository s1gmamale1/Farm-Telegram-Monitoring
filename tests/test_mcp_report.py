"""Tests for the daily AI-fix report wiring in watcherdog.mcp_watcher (skill 2).

Covers the schedule math (_seconds_until_daily) and the flush-then-clear
behaviour (startup catch-up + end-of-day), including dry-run not clearing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from watcherdog import daily_report, mcp_watcher
from watcherdog.config import Config


class FakeClient:
    """Records send_message calls; optionally fails them."""

    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, target, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((target, text))


def _cfg(tmp_path):
    return Config({"DAILY_ERRORS_PATH": str(tmp_path / "daily.jsonl"),
                   "DAILY_REPORT_TIME": "23:59"})


# --- _seconds_until_daily ---------------------------------------------------

def test_seconds_until_daily_later_today():
    now = datetime(2026, 6, 2, 10, 0, 0)
    assert mcp_watcher._seconds_until_daily(now, "23:59") == (13 * 3600 + 59 * 60)


def test_seconds_until_daily_rolls_to_tomorrow():
    now = datetime(2026, 6, 2, 23, 59, 30)  # past today's 23:00
    secs = mcp_watcher._seconds_until_daily(now, "23:00")
    assert 23 * 3600 < secs <= 24 * 3600


def test_seconds_until_daily_bad_time_falls_back_to_2359():
    now = datetime(2026, 6, 2, 0, 0, 0)
    assert mcp_watcher._seconds_until_daily(now, "not-a-time") == (23 * 3600 + 59 * 60)


# --- flush_daily_report -----------------------------------------------------

def test_flush_sends_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    daily_report.record(cfg.daily_errors_path, panel="P1", error="e", fix="f")
    client = FakeClient()
    sent = asyncio.run(mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=True))
    assert sent is True
    assert len(client.sent) == 1 and "auto-fixed" in client.sent[0][1]
    assert daily_report.has_pending(cfg.daily_errors_path) is False  # cleared


def test_flush_empty_log_is_noop(tmp_path):
    cfg = _cfg(tmp_path)
    client = FakeClient()
    sent = asyncio.run(mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=True))
    assert sent is False
    assert client.sent == []


def test_flush_dry_run_does_not_clear(tmp_path):
    cfg = _cfg(tmp_path)
    daily_report.record(cfg.daily_errors_path, panel="P1", error="e", fix="f")
    client = FakeClient()
    asyncio.run(mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=False))
    assert client.sent == []  # nothing actually sent
    assert daily_report.has_pending(cfg.daily_errors_path) is True  # kept for next time


def test_flush_send_failure_keeps_log(tmp_path):
    cfg = _cfg(tmp_path)
    daily_report.record(cfg.daily_errors_path, panel="P1", error="e", fix="f")
    client = FakeClient(fail=True)
    sent = asyncio.run(mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=True))
    assert sent is False
    assert daily_report.has_pending(cfg.daily_errors_path) is True  # not cleared on failure
