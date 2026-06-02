"""Tests for watcherdog.alerter — formatting and the two alert sinks."""

from __future__ import annotations

import asyncio
import threading
import urllib.error

import pytest

from watcherdog import alerter
from watcherdog.alerter import (
    TelegramAlerter,
    UserClientAlerter,
    _fmt_duration,
    format_alert,
    format_alert_oneline,
    format_recovery_oneline,
    format_silence_oneline,
)

ANALYSIS = {
    "severity": "critical",
    "summary": "Bot crashed on launch",
    "root_cause": "Steam Guard prompt",
    "fix": "Re-authenticate the account",
}


# --- formatting -------------------------------------------------------------

def test_format_alert_includes_all_sections():
    out = format_alert("SinFermera3", "critical", ANALYSIS, "raw error text")
    assert "CRITICAL" in out
    assert "SinFermera3" in out
    assert "Bot crashed on launch" in out
    assert "Steam Guard prompt" in out
    assert "Re-authenticate the account" in out
    assert "raw error text" in out


def test_format_alert_truncates_long_excerpt():
    long_excerpt = "x" * 5000
    out = format_alert("bot", "high", {}, long_excerpt)
    assert "…" in out          # truncation marker present
    assert len(out) < 2000     # well under Telegram's 4096 limit


def test_format_alert_handles_missing_analysis():
    out = format_alert("bot", "low", None, "")
    assert "bot" in out
    assert "LOW" in out


def test_format_alert_oneline_is_single_line_and_bounded():
    out = format_alert_oneline("SinFermera3", "high", ANALYSIS)
    assert "\n" not in out
    assert len(out) <= 900
    assert "SinFermera3" in out


def test_silence_and_recovery_oneliners():
    assert "SILENT" in format_silence_oneline("bot", 1800)
    assert "back online" in format_recovery_oneline("bot")


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (1800, "30 min"),
        (7200, "2h 0m"),
        (90000, "1d 1h"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert _fmt_duration(seconds) == expected


# --- TelegramAlerter.send (urllib mocked at the _post layer) ----------------

def _alerter():
    return TelegramAlerter("123:abc", "42", attempts=3)


def test_send_success(monkeypatch):
    a = _alerter()
    monkeypatch.setattr(a, "_post", lambda text: {"ok": True})
    assert a.send("hi") is True


def test_send_not_ok_returns_false_without_retry(monkeypatch):
    a = _alerter()
    calls = []
    monkeypatch.setattr(a, "_post", lambda text: calls.append(text) or {"ok": False})
    assert a.send("hi") is False
    assert len(calls) == 1  # an explicit not-ok response is not retried


def test_send_4xx_stops_early(monkeypatch):
    a = _alerter()
    calls = []

    def boom(text):
        calls.append(text)
        raise urllib.error.HTTPError("url", 400, "bad request", None, None)

    monkeypatch.setattr(a, "_post", boom)
    assert a.send("hi") is False
    assert len(calls) == 1  # a 400 will not fix itself -> no retries


def test_send_retries_transient_then_succeeds(monkeypatch):
    from unittest.mock import Mock

    a = _alerter()
    monkeypatch.setattr(alerter.time, "sleep", lambda s: None)  # no real backoff
    post = Mock(side_effect=[Exception("temporary glitch"), {"ok": True}])
    monkeypatch.setattr(a, "_post", post)
    assert a.send("hi") is True
    assert post.call_count == 2


def test_send_gives_up_after_all_attempts(monkeypatch):
    a = TelegramAlerter("123:abc", "42", attempts=2)
    monkeypatch.setattr(alerter.time, "sleep", lambda s: None)
    calls = []

    def boom(text):
        calls.append(text)
        raise Exception("network down")

    monkeypatch.setattr(a, "_post", boom)
    assert a.send("hi") is False
    assert len(calls) == 2


def test_send_alert_formats_then_sends(monkeypatch):
    a = _alerter()
    sent = {}

    def fake_send(text):
        sent["text"] = text
        return True

    monkeypatch.setattr(a, "send", fake_send)
    assert a.send_alert("bot", "high", ANALYSIS, "raw") is True
    assert "bot" in sent["text"]


# --- UserClientAlerter (real running event loop in a thread) ----------------

@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


def test_user_client_alerter_sends(running_loop):
    sent = []

    class FakeClient:
        async def send_message(self, target, text):
            sent.append((target, text))

    a = UserClientAlerter(FakeClient(), running_loop, "me")
    assert a.send("hello") is True
    assert sent == [("me", "hello")]


def test_user_client_alerter_handles_failure(running_loop):
    class FakeClient:
        async def send_message(self, target, text):
            raise RuntimeError("send failed")

    a = UserClientAlerter(FakeClient(), running_loop, "me")
    assert a.send("hello") is False
