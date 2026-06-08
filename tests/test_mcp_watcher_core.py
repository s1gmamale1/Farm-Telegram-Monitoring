"""Tests for watcherdog.mcp_watcher — pure/pure-async helpers (no live Telegram).

Covers the untested portions: _send, _alert, _seconds_until_daily (already in
test_mcp_report.py), load_watch_chats cache fallback, monitor_once silence
detection, _evaluate_bot below-threshold path, and hourly-report helpers.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watcherdog import mcp_watcher
from watcherdog.config import Config
from watcherdog.storage import IncidentStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path=None, extra=None):
    base = {"DAILY_ERRORS_PATH": str(tmp_path / "daily.jsonl")} if tmp_path else {}
    if extra:
        base.update(extra)
    return Config(base)


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, text))


# ---------------------------------------------------------------------------
# _send — text delivery, dry-run, multi-target
# ---------------------------------------------------------------------------

def test_send_delivers_to_single_target():
    client = _FakeClient()
    ok = asyncio.run(mcp_watcher._send(client, "ibo", "hello", deliver=True))
    assert ok is True
    assert len(client.sent) == 1


def test_send_dry_run_does_not_call_send_message():
    client = _FakeClient()
    ok = asyncio.run(mcp_watcher._send(client, "ibo", "hello", deliver=False))
    assert ok is True
    assert client.sent == []


def test_send_empty_list_returns_false():
    client = _FakeClient()
    ok = asyncio.run(mcp_watcher._send(client, [], "hello", deliver=True))
    assert ok is False


def test_send_multi_target_sends_to_all():
    client = _FakeClient()
    ok = asyncio.run(mcp_watcher._send(client, ["a", "b", "c"], "msg", deliver=True))
    assert ok is True
    assert len(client.sent) == 3


def test_send_multi_target_one_failure_returns_false():
    calls = []

    class _PartialClient:
        async def send_message(self, target, text, **kwargs):
            calls.append(target)
            if target == "bad":
                raise RuntimeError("boom")

    client = _PartialClient()
    ok = asyncio.run(mcp_watcher._send(client, ["good", "bad"], "msg", deliver=True))
    assert ok is False


def test_send_exception_returns_false():
    class _BrokenClient:
        async def send_message(self, target, text, **kwargs):
            raise OSError("disconnected")

    client = _BrokenClient()
    ok = asyncio.run(mcp_watcher._send(client, "ibo", "hello", deliver=True))
    assert ok is False


def test_send_truncates_long_text():
    client = _FakeClient()
    long_text = "x" * 5000
    asyncio.run(mcp_watcher._send(client, "ibo", long_text, deliver=True))
    _, sent_text = client.sent[0]
    assert len(sent_text) <= 4000


# ---------------------------------------------------------------------------
# _alert — bot notifier priority + fallback
# ---------------------------------------------------------------------------

def test_alert_uses_notifier_when_available():
    client = _FakeClient()
    notified = []

    async def fake_notifier(text):
        notified.append(text)
        return True

    state = {"notifier": fake_notifier}
    ok = asyncio.run(mcp_watcher._alert(state, client, "ibo", "error!", deliver=True))
    assert ok is True
    assert notified == ["error!"]
    # Bot handled primary; user client NOT used (no rest targets).
    assert client.sent == []


def test_alert_falls_back_to_user_account_when_notifier_fails():
    client = _FakeClient()

    async def bad_notifier(text):
        return False   # signals failure

    state = {"notifier": bad_notifier}
    ok = asyncio.run(mcp_watcher._alert(state, client, "ibo", "error!", deliver=True))
    assert ok is True
    assert len(client.sent) == 1


def test_alert_notifier_exception_falls_back():
    client = _FakeClient()

    async def raising_notifier(text):
        raise RuntimeError("bot down")

    state = {"notifier": raising_notifier}
    ok = asyncio.run(mcp_watcher._alert(state, client, "ibo", "error!", deliver=True))
    assert ok is True
    assert len(client.sent) == 1


def test_alert_no_notifier_sends_direct():
    client = _FakeClient()
    state = {}
    ok = asyncio.run(mcp_watcher._alert(state, client, "ibo", "msg", deliver=True))
    assert ok is True
    assert len(client.sent) == 1


def test_alert_multi_target_bot_does_primary_user_does_rest():
    """When bot delivers to primary, the USER account still handles rest."""
    client = _FakeClient()
    notified = []

    async def fake_notifier(text):
        notified.append(text)
        return True

    state = {"notifier": fake_notifier}
    targets = ["primary", "secondary", "tertiary"]
    ok = asyncio.run(mcp_watcher._alert(state, client, targets, "alert!", deliver=True))
    # Bot handled primary.
    assert len(notified) == 1
    # User account sent to secondary + tertiary.
    assert len(client.sent) == 2


def test_alert_empty_target_list_returns_false():
    client = _FakeClient()
    ok = asyncio.run(mcp_watcher._alert({}, client, [], "msg", deliver=True))
    assert ok is False


# ---------------------------------------------------------------------------
# _panel_target
# ---------------------------------------------------------------------------

def test_panel_target_with_valid_entity():
    ent = SimpleNamespace(id=100)
    with patch("watcherdog.mcp_watcher.get_peer_id", return_value=100):
        result = mcp_watcher._panel_target(ent, "fallback")
    assert result == 100


def test_panel_target_none_entity_returns_bot():
    result = mcp_watcher._panel_target(None, "fallback_bot")
    assert result == "fallback_bot"


def test_panel_target_exception_returns_bot():
    ent = SimpleNamespace()  # no id → get_peer_id will raise

    with patch("watcherdog.mcp_watcher.get_peer_id", side_effect=ValueError("bad")):
        result = mcp_watcher._panel_target(ent, "fallback")
    assert result == "fallback"


# ---------------------------------------------------------------------------
# _offer_card
# ---------------------------------------------------------------------------

def test_offer_card_no_poster_returns_none():
    state = {}
    result = asyncio.run(
        mcp_watcher._offer_card(state, "title", [{"label": "x", "key": "x"}],
                                panel_target="bot"))
    assert result is None


def test_offer_card_poster_called_and_returns_message():
    fake_msg = object()

    async def fake_poster(title, options, *, panel_target):
        return fake_msg

    state = {"post_card": fake_poster}
    result = asyncio.run(
        mcp_watcher._offer_card(state, "title", [{"label": "x", "key": "x"}],
                                panel_target="bot"))
    assert result is fake_msg


def test_offer_card_poster_exception_returns_none():
    async def boom(title, options, *, panel_target):
        raise RuntimeError("post failed")

    state = {"post_card": boom}
    result = asyncio.run(
        mcp_watcher._offer_card(state, "title", [], panel_target="bot"))
    assert result is None


# ---------------------------------------------------------------------------
# _append_chat_log
# ---------------------------------------------------------------------------

def test_append_chat_log_writes_to_file(tmp_path):
    log_path = str(tmp_path / "chat.log")
    mcp_watcher._append_chat_log(log_path, "hello", "world")
    content = open(log_path, encoding="utf-8").read()
    assert "hello" in content
    assert "world" in content


def test_append_chat_log_none_path_is_noop():
    mcp_watcher._append_chat_log(None, "hello", "world")   # must not raise


def test_append_chat_log_creates_parent_dir(tmp_path):
    log_path = str(tmp_path / "nested" / "dir" / "chat.log")
    mcp_watcher._append_chat_log(log_path, "q", "a")
    assert open(log_path, encoding="utf-8").read()  # non-empty


# ---------------------------------------------------------------------------
# _seconds_until_daily (duplicate guard — already in test_mcp_report.py but
# kept here so this file is self-contained)
# ---------------------------------------------------------------------------

def test_seconds_until_daily_invalid_format():
    now = datetime(2026, 6, 2, 0, 0, 0)
    # Bad format falls back to 23:59.
    secs = mcp_watcher._seconds_until_daily(now, "bad-time")
    assert secs == 23 * 3600 + 59 * 60


# ---------------------------------------------------------------------------
# _save_cached_chats / _load_cached_chats
# ---------------------------------------------------------------------------

def test_save_cached_chats_creates_file(tmp_path, monkeypatch):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})

    ent = SimpleNamespace(username="panelbot")
    with patch("watcherdog.mcp_watcher.get_peer_id", return_value=42):
        mcp_watcher._save_cached_chats(cfg, [("Panel#1", ent)])

    cache_path = mcp_watcher._farms_cache_path(cfg)
    data = json.loads(open(cache_path, encoding="utf-8").read())
    assert data[0]["name"] == "Panel#1"
    assert data[0]["id"] == 42


def test_save_cached_chats_oserror_is_silent(tmp_path, monkeypatch):
    cfg = Config({"DB_PATH": "/nonexistent_dir/sub/monitor.db"})
    ent = SimpleNamespace(username="bot")
    with patch("watcherdog.mcp_watcher.get_peer_id", return_value=1):
        mcp_watcher._save_cached_chats(cfg, [("P", ent)])   # must not raise


def test_load_cached_chats_returns_empty_when_no_file(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})

    class _MockClient:
        async def get_entity(self, eid):
            raise ValueError("no entity")

    chats = asyncio.run(mcp_watcher._load_cached_chats(_MockClient(), cfg))
    assert chats == []


def test_load_cached_chats_resolves_entities(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    cache_path = mcp_watcher._farms_cache_path(cfg)

    rows = [{"id": 42, "name": "Panel#1", "username": None}]
    import os; os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)

    ent = SimpleNamespace(first_name="Panel", last_name=None, title=None,
                          username=None, id=42)

    class _MockClient:
        async def get_entity(self, eid):
            return ent

    with patch("watcherdog.mcp_watcher.tg_tools.entity_name", return_value="Panel#1"):
        chats = asyncio.run(mcp_watcher._load_cached_chats(_MockClient(), cfg))
    assert len(chats) == 1
    assert chats[0][0] == "Panel#1"


def test_load_cached_chats_handles_corrupt_json(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    cache_path = mcp_watcher._farms_cache_path(cfg)

    import os; os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        fh.write("not valid json")

    chats = asyncio.run(mcp_watcher._load_cached_chats(None, cfg))
    assert chats == []


# ---------------------------------------------------------------------------
# _hourly_already_sent / _hourly_mark_sent (pure I/O helpers)
# ---------------------------------------------------------------------------

def test_hourly_already_sent_returns_false_when_no_file(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-07 10") is False


def test_hourly_mark_sent_and_already_sent(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    hour_key = "2026-06-07 10"
    mcp_watcher._hourly_mark_sent(cfg, hour_key)
    assert mcp_watcher._hourly_already_sent(cfg, hour_key) is True


def test_hourly_already_sent_different_hour_returns_false(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    mcp_watcher._hourly_mark_sent(cfg, "2026-06-07 10")
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-07 11") is False


# ---------------------------------------------------------------------------
# _status_emoji
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,emoji", [
    ("✅ farming", "✅"),
    ("⚠️ quiet", "⚠️"),
    ("🔴 needs attention", "🔴"),
    ("💀 dead", "💀"),
    ("unknown status", "❓"),
])
def test_status_emoji(status, emoji):
    assert mcp_watcher._status_emoji(status) == emoji


# ---------------------------------------------------------------------------
# _DROP_STATS_RE
# ---------------------------------------------------------------------------

def test_drop_stats_re_matches_drop_stats():
    assert mcp_watcher._DROP_STATS_RE.search("drop stats please")
    assert mcp_watcher._DROP_STATS_RE.search("DROP STATS")
    assert mcp_watcher._DROP_STATS_RE.search("show drops stats")


def test_drop_stats_re_does_not_match_unrelated():
    assert not mcp_watcher._DROP_STATS_RE.search("what is the status?")
    assert not mcp_watcher._DROP_STATS_RE.search("drop the ball")


# ---------------------------------------------------------------------------
# global no-AI mode
# ---------------------------------------------------------------------------

def test_disable_ai_evaluate_bot_never_calls_agent_or_ollama(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",
        "AGENT_ACTIONS_ENABLED": "true",
        "MIN_SEVERITY": "low",
        "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()

    def fail_analyzer(*_a, **_k):
        raise AssertionError("Ollama analyzer must not be called in DISABLE_AI mode")

    async def fail_agent(*_a, **_k):
        raise AssertionError("agent model must not be called in DISABLE_AI mode")

    async def no_saved_fix(*_a, **_k):
        return {"status": "unknown"}

    monkeypatch.setattr(mcp_watcher, "analyze_message", fail_analyzer)
    monkeypatch.setattr(mcp_watcher.agent, "answer", fail_agent)
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", no_saved_fix)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {"system_prompt": "would normally enable agent"},
        "ibo", "SinFermera9", "Got an error while launching accounts.",
        time.time(), asyncio.new_event_loop(), deliver=True, ent=None))

    assert client.sent
    assert "Got an error while launching accounts" in client.sent[0][1]
    store.close()


def test_benign_drop_collection_error_does_not_alert(tmp_path, monkeypatch):
    # A routine, self-healing "Error collecting drop" must be recorded but NOT
    # escalated to a HIGH alert — even in DISABLE_AI mode at the default
    # MIN_SEVERITY=high, where every classified error otherwise defaults to high.
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",
        "AGENT_ACTIONS_ENABLED": "true",
        "MIN_SEVERITY": "high",
        "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()

    async def no_saved_fix(*_a, **_k):
        return {"status": "unknown"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", no_saved_fix)

    text = (
        "[SinFermera21] Collected drop on 3 accounts: a, b, c.\n"
        "Farmed this week: 82/159\n"
        "Starting next batch...\n"
        "Error collecting drop on: fqekslic11w."
    )
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {}, "ibo", "SinFermera21", text,
        time.time(), asyncio.new_event_loop(), deliver=True, ent=None))

    assert client.sent == []   # no HIGH alert
    # but it is still recorded (below-threshold) as a low-severity incident
    assert store.last_seen(mcp_watcher.error_hash(text)) is not None
    store.close()


def test_drop_collection_error_with_strong_signal_still_alerts(tmp_path, monkeypatch):
    # The benign downgrade must not mask a real problem riding in the same message.
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",
        "AGENT_ACTIONS_ENABLED": "true",
        "MIN_SEVERITY": "high",
        "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()

    async def no_saved_fix(*_a, **_k):
        return {"status": "unknown"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", no_saved_fix)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {}, "ibo", "SinFermera9",
        "Error collecting drop on: x. Account banned by Steam.",
        time.time(), asyncio.new_event_loop(), deliver=True, ent=None))

    assert client.sent   # banned -> still a HIGH alert
    store.close()


# ---------------------------------------------------------------------------
# incident lifecycle: open on alert, resolve on healthy (end-to-end)
# ---------------------------------------------------------------------------

def test_bot_error_then_healthy_emits_one_resolved(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",            # deterministic HIGH, no Ollama
        "AGENT_ACTIONS_ENABLED": "false",  # skip the auto-fix router -> plain alert
        "MIN_SEVERITY": "high",
        "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {"tracker": tracker}
    loop = asyncio.new_event_loop()

    # 1) error -> HIGH alert + open incident
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.",
        100.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is not None

    # 2) healthy line ("All 4 accounts launched!" classifies as normal) ->
    #    resolve + exactly one ✅ Resolved.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] All 4 accounts launched!",
        280.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is None
    resolved = [t for _, t in client.sent if "✅ Resolved" in t]
    assert len(resolved) == 1
    assert "3 min" in resolved[0]
    store.close()
    tracker.close()


# ---------------------------------------------------------------------------
# _incident_followup_tick — the only timer that ACTS on the world
# ---------------------------------------------------------------------------

def _followup_cfg(tmp_path):
    return _cfg(tmp_path, {
        "INCIDENT_FOLLOWUP_INTERVAL": "900",
        "INCIDENT_GIVEUP_MINUTES": "60",   # 3600s
        "INCIDENT_MAX_FIX_RETRIES": "2",
    })


def _capture_alerts(monkeypatch):
    """Replace _alert with a capture stub; return the list it appends to."""
    msgs = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        msgs.append(text)
        return True

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    return msgs


def test_followup_tick_giveup_escalates_panel_needs_pc(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "Panel1", "panel:Panel1", "high", "PC OFF",
                 fixable=False, now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    called = []
    async def spy_fix(*a, **k):
        called.append(a)
        return {"status": "failed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", spy_fix)

    # now == giveup_s (3600) -> the incident is past the give-up window.
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 3600.0, deliver=True))

    assert tracker.open_for_bot("panel", "Panel1") is None   # escalated → off open_list
    assert tracker.open_list() == []
    escalated = [m for m in msgs if "❌" in m]
    assert len(escalated) == 1
    assert "needs PC" in escalated[0]
    assert called == []   # give-up never attempts a fix
    tracker.close()


def test_followup_tick_refix_attempts_fix_when_delivering(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h", "high", "boom",
                 fixable=True, raw_excerpt="boom raw", now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    called = []
    async def spy_fix(client_, cfg_, bot, text, **k):
        called.append((bot, text))
        return {"status": "failed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", spy_fix)

    # now == followup interval (900), well before give-up (3600).
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 900.0, deliver=True))

    assert len(called) == 1            # the known fix WAS re-attempted
    assert called[0] == ("Bot1", "boom raw")
    row = tracker.open_for_bot("bot_error", "Bot1")
    assert row is not None             # still open (a refix nags, doesn't resolve)
    assert row["fix_retries"] == 1     # bumped
    followups = [m for m in msgs if "⏳" in m]
    assert len(followups) == 1
    assert "retry" in followups[0].lower()   # retrying=True copy
    tracker.close()


def test_followup_tick_refix_skips_fix_in_dry_run(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h", "high", "boom",
                 fixable=True, raw_excerpt="boom raw", now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    called = []
    async def spy_fix(*a, **k):
        called.append(a)   # MUST NOT happen under dry-run
        return {"status": "failed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", spy_fix)

    # Same as above but deliver=False: no real buttons may be pressed.
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 900.0, deliver=False))

    assert called == []                # the I1 fix: no fix attempt in dry-run
    row = tracker.open_for_bot("bot_error", "Bot1")
    assert row is not None
    assert row["fix_retries"] == 0     # not bumped (no attempt recorded)
    followups = [m for m in msgs if "⏳" in m]
    assert len(followups) == 1         # still nags
    tracker.close()


# ---------------------------------------------------------------------------
# flush_daily_report (integration with daily_report module)
# ---------------------------------------------------------------------------

def test_flush_daily_report_state_passed_through(tmp_path):
    from watcherdog import daily_report
    cfg = _cfg(tmp_path)
    daily_report.record(cfg.daily_errors_path, panel="P1", error="err", fix="fix")
    client = _FakeClient()
    state = {}   # no notifier
    ok = asyncio.run(
        mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=True, state=state))
    assert ok is True


def test_flush_daily_report_empty_log_returns_false(tmp_path):
    cfg = _cfg(tmp_path)
    client = _FakeClient()
    ok = asyncio.run(
        mcp_watcher.flush_daily_report(client, cfg, "ibo", deliver=True, state={}))
    assert ok is False


# ---------------------------------------------------------------------------
# _cant_find_match_minutes (regex helper)
# ---------------------------------------------------------------------------

def test_cant_find_match_minutes_matches_typical():
    text = "Can't find match in 45 minutes, changing batch"
    assert mcp_watcher._cant_find_match_minutes(text) == 45


def test_cant_find_match_minutes_cannot_variant():
    text = "cannot find match in 120 minutes, changing batch"
    assert mcp_watcher._cant_find_match_minutes(text) == 120


def test_cant_find_match_minutes_apostrophe_variant():
    text = "Can't find match in 30 minutes; changing batch"
    assert mcp_watcher._cant_find_match_minutes(text) == 30


def test_cant_find_match_minutes_no_match_returns_none():
    assert mcp_watcher._cant_find_match_minutes("all good") is None
    assert mcp_watcher._cant_find_match_minutes("") is None
    assert mcp_watcher._cant_find_match_minutes(None) is None


def test_cant_find_match_minutes_case_insensitive():
    text = "CAN'T FIND MATCH IN 60 MINUTES, CHANGING BATCH"
    assert mcp_watcher._cant_find_match_minutes(text) == 60


# ---------------------------------------------------------------------------
# _seconds_until_daily — boundary cases
# ---------------------------------------------------------------------------

def test_seconds_until_daily_future_today():
    now = datetime(2026, 6, 7, 10, 0, 0)
    secs = mcp_watcher._seconds_until_daily(now, "23:00")
    assert secs == 13 * 3600


def test_seconds_until_daily_past_target_schedules_tomorrow():
    now = datetime(2026, 6, 7, 23, 30, 0)
    secs = mcp_watcher._seconds_until_daily(now, "22:00")
    # 22:00 has passed today; next one is 22.5h away
    assert abs(secs - 22.5 * 3600) < 10


def test_seconds_until_daily_out_of_range_fallback():
    now = datetime(2026, 6, 7, 0, 0, 0)
    secs = mcp_watcher._seconds_until_daily(now, "25:61")
    assert secs == 23 * 3600 + 59 * 60


def test_seconds_until_daily_exactly_on_time_schedules_tomorrow():
    now = datetime(2026, 6, 7, 22, 0, 0)
    secs = mcp_watcher._seconds_until_daily(now, "22:00")
    assert secs == 24 * 3600


# ---------------------------------------------------------------------------
# _resolve_session_string
# ---------------------------------------------------------------------------

def test_resolve_session_string_returns_explicit_string(tmp_path):
    cfg = Config({"TELEGRAM_SESSION_STRING": "mySuperString",
                  "TELEGRAM_SESSION": str(tmp_path / "watcher"),
                  "DB_PATH": str(tmp_path / "monitor.db")})
    result = mcp_watcher._resolve_session_string(cfg)
    assert result == "mySuperString"


def test_resolve_session_string_returns_none_when_file_exists(tmp_path):
    session_file = tmp_path / "watcher.session"
    session_file.write_text("")
    cfg = Config({"TELEGRAM_SESSION": str(tmp_path / "watcher"),
                  "DB_PATH": str(tmp_path / "monitor.db")})
    result = mcp_watcher._resolve_session_string(cfg)
    assert result is None


def test_resolve_session_string_returns_none_no_mcp_env(tmp_path):
    cfg = Config({"TELEGRAM_SESSION": str(tmp_path / "nonexistent"),
                  "DB_PATH": str(tmp_path / "monitor.db"),
                  "TELEGRAM_MCP_DIR": str(tmp_path / "mcp_dir")})
    # No .env in mcp_dir → returns None
    result = mcp_watcher._resolve_session_string(cfg)
    assert result is None


def test_resolve_session_string_reuses_mcp_env(tmp_path):
    mcp_dir = tmp_path / "mcp_dir"
    mcp_dir.mkdir()
    (mcp_dir / ".env").write_text("TELEGRAM_SESSION_STRING=fromMcpEnv\n")
    cfg = Config({"TELEGRAM_SESSION": str(tmp_path / "nonexistent"),
                  "DB_PATH": str(tmp_path / "monitor.db"),
                  "TELEGRAM_MCP_DIR": str(mcp_dir)})
    result = mcp_watcher._resolve_session_string(cfg)
    assert result == "fromMcpEnv"


# ---------------------------------------------------------------------------
# load_watch_chats — folder resolution + cache fallback
# ---------------------------------------------------------------------------

def test_load_watch_chats_falls_back_to_cache_on_api_error(tmp_path, monkeypatch):
    """GetDialogFilters failure → _load_cached_chats is called."""
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db"),
                  "WATCH_FOLDER": "Farms"})

    async def boom(*a, **kw):
        raise RuntimeError("API unavailable")

    async def fake_cached(client, cfg):
        return [("Panel#1", "ent1")]

    monkeypatch.setattr(mcp_watcher, "_load_cached_chats", fake_cached)

    class _Client:
        async def __call__(self, req):
            raise RuntimeError("API unavailable")
        async def get_entity(self, p):
            return p

    result = asyncio.run(mcp_watcher.load_watch_chats(_Client(), cfg))
    assert result == [("Panel#1", "ent1")]


def test_load_watch_chats_falls_back_when_folder_not_found(tmp_path, monkeypatch):
    """Folder not found in filter list → cache fallback."""
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db"),
                  "WATCH_FOLDER": "NonExistentFolder"})

    async def fake_cached(client, cfg):
        return []

    monkeypatch.setattr(mcp_watcher, "_load_cached_chats", fake_cached)

    fake_filter = SimpleNamespace(id=1, title="SomeOtherFolder",
                                  pinned_peers=[], include_peers=[])
    fake_res = SimpleNamespace(filters=[fake_filter])

    with patch("watcherdog.mcp_watcher.tg_tools.filter_title", return_value="SomeOtherFolder"):
        class _Client:
            async def __call__(self, req):
                return fake_res
            async def get_entity(self, p):
                return p

        result = asyncio.run(mcp_watcher.load_watch_chats(_Client(), cfg))
    assert result == []
