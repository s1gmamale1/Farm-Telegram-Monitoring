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


# --- missing full-form formatters -------------------------------------------

def test_format_silence_alert_contains_bot_and_duration():
    out = alerter.format_silence_alert("SinFermera3", 3600)
    assert "SinFermera3" in out
    assert "SILENT" in out
    # Duration should be in the output (1 hour).
    assert "1h" in out


def test_format_recovery_alert_contains_bot():
    out = alerter.format_recovery_alert("SinFermera3")
    assert "SinFermera3" in out
    assert "online" in out.lower() or "back" in out.lower()


def test_format_recurring_alert_includes_count_and_bots():
    group = {
        "count": 5,
        "bots": ["SinFermera1", "SinFermera2"],
        "summary": "proxy timeout",
        "raw_excerpt": "",
    }
    out = alerter.format_recurring_alert(group, window_minutes=30)
    assert "5" in out
    assert "30" in out
    assert "SinFermera1" in out
    assert "proxy timeout" in out


def test_format_recurring_alert_falls_back_to_excerpt():
    """When summary is empty, falls back to raw_excerpt."""
    group = {"count": 2, "bots": [], "summary": "", "raw_excerpt": "Connection refused"}
    out = alerter.format_recurring_alert(group, window_minutes=60)
    assert "Connection refused" in out


def test_format_recurring_alert_no_bots():
    group = {"count": 3, "bots": None, "summary": "err", "raw_excerpt": ""}
    out = alerter.format_recurring_alert(group, window_minutes=60)
    assert "?" in out  # fallback for empty bot list


# --- TelegramAlerter._post thread_id handling --------------------------------

def test_post_includes_thread_id_when_set(monkeypatch):
    """A numeric thread_id must be converted to int and included in the payload."""
    import json as _json

    a = TelegramAlerter("tok", "99", thread_id="42", attempts=1)
    payloads = []

    def fake_urlopen(req, timeout=None):
        payloads.append(_json.loads(req.data.decode()))

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        return _R()

    monkeypatch.setattr(alerter.urllib.request, "urlopen", fake_urlopen)
    a.send("hi")
    assert payloads[0]["message_thread_id"] == 42


def test_post_skips_invalid_thread_id(monkeypatch):
    """A non-numeric thread_id must be silently dropped (no crash)."""
    import json as _json

    a = TelegramAlerter("tok", "99", thread_id="not-a-number", attempts=1)
    payloads = []

    def fake_urlopen(req, timeout=None):
        payloads.append(_json.loads(req.data.decode()))

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        return _R()

    monkeypatch.setattr(alerter.urllib.request, "urlopen", fake_urlopen)
    a.send("hi")
    assert "message_thread_id" not in payloads[0]


# --- 429 rate-limit: must retry (not stop early) ----------------------------

def test_send_retries_on_429(monkeypatch):
    a = TelegramAlerter("tok", "99", attempts=3)
    monkeypatch.setattr(alerter.time, "sleep", lambda s: None)
    calls = []

    def boom(text):
        calls.append(text)
        raise urllib.error.HTTPError("url", 429, "Too Many Requests", None, None)

    monkeypatch.setattr(a, "_post", boom)
    result = a.send("hi")
    assert result is False
    assert len(calls) == 3  # 429 is explicitly excluded from the "stop early" rule


# --- 5xx server errors: must retry -----------------------------------------

def test_send_retries_on_5xx(monkeypatch):
    a = TelegramAlerter("tok", "99", attempts=3)
    monkeypatch.setattr(alerter.time, "sleep", lambda s: None)
    calls = []

    def boom(text):
        calls.append(text)
        raise urllib.error.HTTPError("url", 503, "Service Unavailable", None, None)

    monkeypatch.setattr(a, "_post", boom)
    result = a.send("hi")
    assert result is False
    assert len(calls) == 3  # 5xx are transient; must retry all attempts


# --- _fmt_duration boundary: zero seconds -----------------------------------

def test_fmt_duration_zero_seconds():
    assert _fmt_duration(0) == "0 min"


def test_fmt_duration_exactly_one_hour():
    assert _fmt_duration(3600) == "1h 0m"


# --- UserClientAlerter.send_alert calls format_alert then send --------------

def test_user_client_alerter_send_alert_formats_and_sends(running_loop):
    sent = []

    class FakeClient:
        async def send_message(self, target, text):
            sent.append(text)

    a = UserClientAlerter(FakeClient(), running_loop, "me")
    a.send_alert("SinFermera3", "critical", ANALYSIS, "raw excerpt")
    assert len(sent) == 1
    assert "SinFermera3" in sent[0]
    assert "CRITICAL" in sent[0]
    assert "raw excerpt" in sent[0]


def test_incident_resolved_self_healed():
    from watcherdog.alerter import format_incident_resolved
    msg = format_incident_resolved("SinFermera19", 180, we_fixed=False)
    assert "SinFermera19" in msg
    assert "✅" in msg
    assert "3 min" in msg
    assert "on its own" in msg


def test_incident_resolved_we_fixed():
    from watcherdog.alerter import format_incident_resolved
    msg = format_incident_resolved("SinFermera19", 60, we_fixed=True)
    assert "WatcherDog" in msg


def test_incident_followup_retrying():
    from watcherdog.alerter import format_incident_followup
    msg = format_incident_followup("SinFermera19", "launch error", 900, retrying=True)
    assert "⏳" in msg
    assert "still unresolved" in msg
    assert "retry" in msg.lower()


def test_incident_escalated_needs_pc():
    from watcherdog.alerter import format_incident_escalated
    msg = format_incident_escalated("SinFermera3", "PC OFF", 3600, needs_pc=True)
    assert "❌" in msg
    assert "needs PC" in msg
