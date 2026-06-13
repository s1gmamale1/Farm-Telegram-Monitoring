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

from watcherdog import mcp_watcher, roster
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


@pytest.fixture(autouse=True)
def _clear_panel_state():
    # _PANEL_STATE is process-global; clear it around every test so per-panel FSM
    # state (recover_attempts, flag_alerted, last_action_ts, …) never leaks across
    # tests (mirror test_evaluate_panel.py's _clear_state).
    mcp_watcher._PANEL_STATE.clear()
    yield
    mcp_watcher._PANEL_STATE.clear()


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
# _hourly_already_sent / _save_hourly_state (pure I/O helpers)
# ---------------------------------------------------------------------------

def test_hourly_already_sent_returns_false_when_no_file(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-07 10") is False


def test_hourly_mark_sent_and_already_sent(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    hour_key = "2026-06-07 10"
    mcp_watcher._save_hourly_state(cfg, {"last_hour": hour_key})
    assert mcp_watcher._hourly_already_sent(cfg, hour_key) is True


def test_hourly_already_sent_different_hour_returns_false(tmp_path):
    cfg = Config({"DB_PATH": str(tmp_path / "monitor.db")})
    mcp_watcher._save_hourly_state(cfg, {"last_hour": "2026-06-07 10"})
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-07 11") is False


def test_hourly_report_skips_when_no_target(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, {"HOURLY_REPORT_ENABLED": "true", "DB_PATH": str(tmp_path / "monitor.db")})
    # force an empty target (simulate a truly-unconfigured deploy)
    cfg.hourly_report_chat = ""
    calls = []

    class _C:
        async def get_entity(self, ref):
            calls.append(ref)
            raise AssertionError("must not resolve an empty target")

    ok = asyncio.run(mcp_watcher.run_hourly_report(_C(), cfg, watch=[], deliver=True))
    assert ok is False
    assert calls == []


# ---------------------------------------------------------------------------
# _status_emoji (now lives in roster.status_emoji)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,emoji", [
    ("✅ farming", "✅"),
    ("⚠️ quiet", "⚠️"),
    ("🔴 needs attention", "🔴"),
    ("💀 dead", "💀"),
    ("unknown status", "❓"),
])
def test_status_emoji(status, emoji):
    assert roster.status_emoji(status) == emoji


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


def test_disable_ai_uses_deterministic_severity_not_blanket_high(tmp_path, monkeypatch):
    # In DISABLE_AI mode a captcha must classify CRITICAL (via severity_of), not the
    # old blanket "high". Spy on what severity gets recorded.
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    recorded = []
    orig = store.record
    def spy(bot, severity, *a, **k):
        recorded.append(severity); return orig(bot, severity, *a, **k)
    monkeypatch.setattr(store, "record", spy)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {}, "ibo", "SinFermera9",
        "captcha required to continue", 100.0,
        asyncio.new_event_loop(), deliver=True, ent=None))

    assert "critical" in recorded            # severity_of -> critical, not "high"
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
# Phase B Task 1: freshness-gate the classify-normal incident resolve
# ---------------------------------------------------------------------------

class _FakeTracker:
    """Minimal tracker stub: a bot_error incident is open at ``opened_ts`` and
    records every resolve_open_for_bot call so the test can assert on it."""

    def __init__(self, opened_ts):
        self.opened_ts = opened_ts
        self.resolve_calls = []

    def open_for_bot(self, source, bot):
        if source == "bot_error":
            return {"key": f"bot_error:{bot}", "opened_ts": self.opened_ts,
                    "severity": "high", "raw_excerpt": "boom"}
        return None

    def resolve_open_for_bot(self, bot, resolution, now=None):
        self.resolve_calls.append((bot, resolution, now))
        return {"count": 1, "elapsed": 1.0, "we_fixed": False}


def test_stale_normal_message_does_not_resolve_incident(tmp_path):
    # A silent bot's last (stale) message is a routine drop line. It classifies
    # "normal" but predates the still-open incident → it must NOT resolve it.
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    now = time.time()
    tracker = _FakeTracker(opened_ts=now - 10)        # incident opened 10s ago
    state = {"tracker": tracker}
    date = SimpleNamespace(timestamp=lambda: now - 600)  # message 10 min old (stale)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$",
        now, asyncio.new_event_loop(), deliver=True, ent=None, date=date))

    assert tracker.resolve_calls == []   # stale proof → incident stays open
    store.close()


def test_fresh_normal_message_resolves_incident(tmp_path):
    # A genuinely fresh "normal" message (newer than the open incident) proves
    # the bot recovered → it must resolve the incident exactly once.
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    now = time.time()
    tracker = _FakeTracker(opened_ts=now - 600)       # incident opened 10 min ago
    state = {"tracker": tracker}
    date = SimpleNamespace(timestamp=lambda: now)        # message is now (fresh)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$",
        now, asyncio.new_event_loop(), deliver=True, ent=None, date=date))

    assert len(tracker.resolve_calls) == 1   # fresh proof → resolved once
    assert tracker.resolve_calls[0][0] == "Bot1"
    store.close()


# ---------------------------------------------------------------------------
# Task 4: channel coordination + cross-source resolution
# ---------------------------------------------------------------------------

def test_open_incident_suppresses_duplicate_detection_alert(tmp_path, monkeypatch):
    # A bot_error incident already open → the SAME error (same hash, same severity)
    # for the bot is a true duplicate symptom and must be suppressed (recorded, not
    # re-alerted). The open row must carry the SAME excerpt/severity the gate will
    # compute, or the gate treats it as a new/worse error and alerts (correctly).
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    text = "[Bot1] Got an error while launching accounts."
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, raw_excerpt=text[:1000], now=1.0)  # DISABLE_AI → "high"
    client = _FakeClient()
    state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text, 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent == []          # same error already open → no duplicate 🟠
    store.close()
    tracker.close()


def test_open_panel_incident_does_not_suppress_bot_error(tmp_path):
    # A panel (or silence) incident is a DIFFERENT failure mode/channel; it must
    # NEVER swallow a fresh bot_error alert. F1 regression guard.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "Bot1", "panel:Bot1", "high", "pc off", fixable=False, now=1.0)
    client = _FakeClient()
    state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.", 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent                                  # a panel incident does NOT suppress
    assert tracker.open_for_bot("bot_error", "Bot1") is not None  # new bot_error opened
    store.close()
    tracker.close()


def test_first_detection_still_alerts_and_opens(tmp_path):
    # The FIRST detection (no incident open yet) must still alert + open — the
    # suppression guard only fires on repeats while an incident is open.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.", 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent                                  # alerted
    assert tracker.open_for_bot("bot_error", "Bot1") is not None  # opened
    store.close()
    tracker.close()


def test_open_incident_alerts_on_different_error_and_refreshes(tmp_path):
    # A bot_error incident is open for a FIRST error; a genuinely DIFFERENT error
    # (different hash) for the same bot must NOT be swallowed — it alerts AND the
    # open row is refreshed in place to the new error's summary/excerpt.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high",
                 "launch error", fixable=False, raw_excerpt="boom", now=1.0)
    client = _FakeClient()
    state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Account banned permanently.", 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent                                # NEW distinct error → alerted
    row = tracker.open_for_bot("bot_error", "Bot1")
    assert row is not None and row["status"] == "open"
    assert "Account banned" in (row["summary"] or "")  # refreshed in place
    assert "Account banned" in (row["raw_excerpt"] or "")
    store.close()
    tracker.close()


def test_open_incident_alerts_on_higher_severity_same_hash(tmp_path, monkeypatch):
    # The SAME error text but the analyzer now scores it CRITICAL (severity rose
    # above the open row's). Same-hash but worse → alert + refresh, not suppress.
    from watcherdog.incident_tracker import IncidentTracker
    # Exercises the MODEL path (analyze_message) so the analyzer can RAISE the
    # severity to critical — opt into it explicitly now that the deterministic
    # core is the default (DISABLE_AI defaults ON).
    cfg = _cfg(tmp_path, {"DISABLE_AI": "false", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    text = "[Bot1] Got an error while launching accounts."
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "medium",
                 "launch error", fixable=False, raw_excerpt=text, now=1.0)

    def crit_analyzer(*_a, **_k):
        return {"is_error": True, "severity": "critical",
                "summary": "ACCOUNT BANNED", "root_cause": "", "fix": ""}

    monkeypatch.setattr(mcp_watcher, "analyze_message", crit_analyzer)
    client = _FakeClient()
    state = {"tracker": tracker}

    async def _run():
        # The analyzer path uses loop.run_in_executor, so the loop passed in MUST
        # be the one asyncio.run created (the running loop), not a detached one.
        await mcp_watcher._evaluate_bot(
            client, cfg, store, state, "ibo", "Bot1", text, 50.0,
            asyncio.get_running_loop(), deliver=True, ent=None)

    asyncio.run(_run())
    assert client.sent                                  # severity ROSE → alerted
    row = tracker.open_for_bot("bot_error", "Bot1")
    assert row["severity"] == "critical"                # refreshed to worse severity
    assert row["summary"] == "ACCOUNT BANNED"
    store.close()
    tracker.close()


def test_open_incident_suppresses_same_error_same_severity(tmp_path):
    # The SAME error (same hash) at the SAME severity while open is still a pure
    # duplicate → suppressed (recorded, not re-alerted). Guards the fix did not
    # break the original suppression behaviour.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    text = "[Bot1] Got an error while launching accounts."
    # DISABLE_AI → severity "high", summary = text[:200]. Open the row with the
    # SAME excerpt and severity the gate will compute so it is a true duplicate.
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high",
                 text.strip()[:200], fixable=False, raw_excerpt=text[:1000], now=1.0)
    client = _FakeClient()
    state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text, 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent == []          # same hash + same severity → suppressed
    store.close()
    tracker.close()


def test_dedupe_recurrence_reopens_incident_without_alert(tmp_path):
    # Phase B Task 4: after a resolve, an IDENTICAL error recurring within the
    # dedupe window hits the store.last_seen early-return. Before the fix that
    # return left the bot broken with NO open incident (no followups, no alert).
    # Now the recurrence must RE-OPEN the incident (idempotently, no alert spam)
    # so the follow-up loop keeps tracking it to resolution/escalation.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true",             # deterministic HIGH, no Ollama
        "AGENT_ACTIONS_ENABLED": "false",  # skip the auto-fix router -> plain alert
        "MIN_SEVERITY": "high",
        "DEDUPE_WINDOW": "300",            # non-zero so the second call dedupes
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {"tracker": tracker}
    loop = asyncio.new_event_loop()
    text = "[Bot1] Got an error while launching accounts."

    # 1) First error -> HIGH alert + open incident. The alert path's store.record
    #    stamps last_seen(h) at now=100.0, arming the dedupe window.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text,
        100.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is not None
    first_sent = len(client.sent)
    assert first_sent == 1                # exactly one alert for the first error

    # 2) The bot recovers via a NORMAL message (the realistic resolve path — in
    #    production a bot_error only closes when a FRESH normal line arrives, never
    #    via a direct resolve call). This also moves the per-bot memo OFF the error
    #    hash and ONTO the normal hash, so the recurrence in step 3 is not skipped.
    #    The date must be at/after the incident's opened_ts (Task 1 freshness gate).
    fresh = SimpleNamespace(timestamp=lambda: 150.0)
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$",
        150.0, loop, deliver=True, ent=None, date=fresh))
    assert tracker.open_for_bot("bot_error", "Bot1") is None  # freshness resolve closed it
    after_resolve_sent = len(client.sent)

    # 3) The SAME error recurs within the dedupe window (200.0 - 100.0 = 100s < 300).
    #    The memo now holds the NORMAL hash, so this genuine recurrence is NOT
    #    skipped. No NEW alert (dedupe gate sees the notified error row from step 1),
    #    BUT the incident must be RE-OPENED so followups continue instead of
    #    silently dropping the still-broken bot.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text,
        200.0, loop, deliver=True, ent=None))
    assert len(client.sent) == after_resolve_sent  # dedupe held → no new error alert
    assert tracker.open_for_bot("bot_error", "Bot1") is not None  # re-opened
    store.close()
    tracker.close()


# ---------------------------------------------------------------------------
# Task 6: per-bot memo — skip re-processing the SAME latest message on
# consecutive sweeps (no wasted analysis, no below-threshold re-record spam).
# ---------------------------------------------------------------------------

def test_memo_skips_reprocessing_same_message_consecutively(tmp_path):
    # The SAME error fed twice in a row for one bot: the analysis/record path runs
    # only ONCE; the second identical sight early-returns on the memo.
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
        "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {}
    loop = asyncio.new_event_loop()
    text = "[Bot1] Got an error while launching accounts."

    calls = {"record": 0}
    real_record = store.record

    def counting_record(*a, **k):
        calls["record"] += 1
        return real_record(*a, **k)
    store.record = counting_record  # type: ignore[assignment]

    # First sight: processes fully (classify, alert, record).
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text,
        100.0, loop, deliver=True, ent=None))
    assert calls["record"] == 1
    assert len(client.sent) == 1

    # Second IDENTICAL sight on the next sweep: memo match → early-return, no
    # second analysis/record, no extra alert.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text,
        200.0, loop, deliver=True, ent=None))
    assert calls["record"] == 1          # NOT re-recorded
    assert len(client.sent) == 1         # NOT re-alerted
    store.close()


def test_memo_does_not_skip_a_different_message(tmp_path):
    # A genuinely DIFFERENT message between identical sights is NOT skipped: the
    # memo is keyed on the content hash, so a hash change always processes.
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
        "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {}
    loop = asyncio.new_event_loop()
    err_a = "[Bot1] Got an error while launching accounts."
    err_b = "[Bot1] Account banned permanently."

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", err_a,
        100.0, loop, deliver=True, ent=None))
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", err_b,
        200.0, loop, deliver=True, ent=None))
    # Two distinct messages → two alerts (memo never matched err_b against err_a).
    assert len(client.sent) == 2
    store.close()


def test_memo_first_sight_still_resolves_open_incident(tmp_path):
    # The memo must NOT swallow the FIRST sight of any message: a fresh "normal"
    # message on its first sight must still resolve an open incident (Task 1).
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
        "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {"tracker": tracker}
    loop = asyncio.new_event_loop()

    # error at now=100 → open incident.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.",
        100.0, loop, deliver=True, ent=None))
    assert tracker.open_for_bot("bot_error", "Bot1") is not None

    # FIRST sight of a fresh normal message → resolves (memo was on the error hash).
    fresh = SimpleNamespace(timestamp=lambda: 150.0)
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$",
        150.0, loop, deliver=True, ent=None, date=fresh))
    assert tracker.open_for_bot("bot_error", "Bot1") is None  # resolved on first sight
    store.close()
    tracker.close()


def test_unnotified_below_threshold_row_does_not_suppress_later_real_alert(tmp_path):
    # Bug 2: a below-threshold (notified=False) row for hash H must NOT keep H
    # "fresh" and suppress a later REAL at/above-threshold alert for the same text.
    # The dedupe gate uses notified_only=True, so an un-alerted row never gates.
    cfg = _cfg(tmp_path, {
        "DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
        "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "300",
    })
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    state = {}
    loop = asyncio.new_event_loop()
    text = "[Bot1] Got an error while launching accounts."
    h = mcp_watcher.error_hash(text)

    # Pre-seed a below-threshold, un-alerted row for H at t=100 (as the
    # below-threshold path would record). Under last_seen(h) this would make H
    # "fresh" and suppress the real alert below; under notified_only it must not.
    store.record("Bot1", "low", {"summary": "x"}, h, text, notified=False, ts=100.0)

    # A REAL high-severity sight of the SAME text within the dedupe window.
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1", text,
        200.0, loop, deliver=True, ent=None))
    assert client.sent          # NOT suppressed by the stale un-alerted row → ALERTS
    store.close()


def test_panel_healthy_resolves_open_incident(tmp_path):
    # open a panel incident (paired with the flag latch, as the real PC-OFF path
    # does), then a healthy status card → exactly one ✅ Resolved closure.
    from watcherdog import panel_rules
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {})
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "SinFermera7", "panel:SinFermera7", "high",
                 "PC OFF / unreachable", fixable=False, now=100.0)
    # Mirror production: a panel incident is always opened alongside a latch
    # (PC-OFF sets flag_alerted) — that latch is what makes the recovery sweep an
    # "episode" worth resolving (F2 had_episode gate).
    ps = panel_rules.PanelState()
    ps.flag_alerted = True
    mcp_watcher._PANEL_STATE["SinFermera7"] = ps
    client = _FakeClient()
    state = {"tracker": tracker}
    healthy = ("📊 Panel status:\n├ 👥 Launched: 4 accounts\n├ 🟢 Status: LIVE\n"
               "├ Map: de_nuke\n└ Score: [1:0]")
    date = SimpleNamespace(timestamp=lambda: time.time() - 1.0)
    asyncio.run(mcp_watcher._evaluate_panel(
        client, cfg, "SinFermera7", object(), healthy, date,
        deliver=True, state=state, target="ibo"))
    assert tracker.open_list_for_bot("SinFermera7") == []
    resolved = [t for _, t in client.sent if "✅ Resolved" in t]
    assert len(resolved) == 1
    # EXACTLY one closure message overall (no stray "✅ back online" beside it).
    check_marks = [t for _, t in client.sent if "✅" in t]
    assert len(check_marks) == 1
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

    # Bot1 must be in the watch roster so the refix can resolve its panel entity;
    # without it the fix now (correctly) skips the refix as off-roster.
    state = {"tracker": tracker, "watch": [("Bot1", object())]}
    # now == followup interval (900), well before give-up (3600).
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", state, 900.0, deliver=True))

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


def test_followup_refix_passes_roster_entity_as_chat(tmp_path, monkeypatch):
    """A re-fix must drive the REAL panel entity from the watch roster, not the
    bot's display name — passing chat=<entity> (never None) to try_auto_fix."""
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "SinFermera2", "bot_error:SinFermera2:h", "high",
                 "boom", fixable=True, raw_excerpt="boom raw", now=0.0)
    client = _FakeClient()
    _capture_alerts(monkeypatch)

    sentinel_entity = object()
    captured = {}
    async def spy_fix(client_, cfg_, bot, text, **k):
        captured["bot"] = bot
        captured["chat"] = k.get("chat", "MISSING")
        return {"status": "failed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", spy_fix)

    state = {"tracker": tracker, "watch": [("SinFermera2", sentinel_entity)]}
    # now == followup interval (900), well before give-up (3600): refix-eligible.
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", state, 900.0, deliver=True))

    assert captured.get("bot") == "SinFermera2"
    # The bug today: chat is None -> try_auto_fix resolves the DISPLAY NAME as a
    # username. The fix passes the roster entity instead.
    assert captured.get("chat") is sentinel_entity
    tracker.close()


def test_followup_refix_skipped_when_bot_not_in_roster(tmp_path, monkeypatch):
    """When the bot isn't in the watch roster we can't resolve its panel entity —
    the tick must DEGRADE to a plain nag: no try_auto_fix call, retry budget intact."""
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "SinFermera2", "bot_error:SinFermera2:h", "high",
                 "boom", fixable=True, raw_excerpt="boom raw", now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    called = []
    async def spy_fix(*a, **k):
        called.append(a)   # MUST NOT happen — bot is off-roster
        return {"status": "failed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", spy_fix)

    state = {"tracker": tracker, "watch": []}   # bot absent from roster
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", state, 900.0, deliver=True))

    assert called == []                # refix skipped: no off-roster button press
    row = tracker.open_for_bot("bot_error", "SinFermera2")
    assert row is not None
    assert row["fix_retries"] == 0     # budget NOT burned — degraded to plain nag
    followups = [m for m in msgs if "⏳" in m]
    assert len(followups) == 1         # still nags
    tracker.close()


# ---------------------------------------------------------------------------
# Task 8: the tick mutates incidents BY ROW ID and skips rows resolved during a
# prior action's await — so a monitor sweep that resolves+reopens a key mid-tick
# never lands a stale mutation on the fresh row (budget pre-burned, stale alert).
# ---------------------------------------------------------------------------

def test_followup_tick_marks_same_row_by_id_on_normal_path(tmp_path, monkeypatch):
    # The non-race baseline: a still-open incident must be followed up exactly as
    # before, bumping THE SAME row's update_count (proof the tick re-fetched and
    # mutated by id, not just by key).
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=0.0)
    iid = tracker.open_for_bot("silence", "Bot1")["id"]
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 900.0, deliver=True))

    row = tracker.get_open_by_id(iid)
    assert row is not None
    assert row["update_count"] == 1           # SAME row bumped
    assert row["last_update_ts"] == 900.0
    followups = [m for m in msgs if "⏳" in m]
    assert len(followups) == 1
    tracker.close()


def test_followup_tick_skips_followup_for_preresolved_row(tmp_path, monkeypatch):
    # If the snapshotted incident is already resolved by the time the tick goes to
    # act on it (resolved during a prior action's await), the tick must SKIP it:
    # no stale ⏳ nag, no mutation. Simulate by resolving the row, then planning
    # the action manually and feeding the stale snapshot into the tick.
    from watcherdog.incident_tracker import IncidentTracker, incident_followup_step
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    # Plan the action against the OPEN row, capturing its (now valid) snapshot...
    planned = incident_followup_step(
        tracker, 900.0, followup_interval_s=900, giveup_s=3600, max_fix_retries=2)
    assert [a["kind"] for a in planned] == ["followup"]
    snap_id = planned[0]["row"]["id"]

    # ...then resolve the incident (as a monitor sweep would, mid-await) and patch
    # the planner so the tick re-uses the STALE snapshot.
    tracker.resolve_by_bot("silence", "Bot1", "self_healed", now=850.0)
    monkeypatch.setattr(mcp_watcher, "incident_followup_step", lambda *a, **k: planned)

    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 900.0, deliver=True))

    # The resolved snapshot id is no longer open → tick skips it.
    assert tracker.get_open_by_id(snap_id) is None
    assert [m for m in msgs if "⏳" in m] == []   # NO stale follow-up alert
    tracker.close()


def test_followup_tick_skips_giveup_for_preresolved_row(tmp_path, monkeypatch):
    # Same skip property on the give-up branch: a snapshot that resolved mid-await
    # must NOT trigger the ❌ escalation alert or burn the escalate path.
    from watcherdog.incident_tracker import IncidentTracker, incident_followup_step
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "Panel1", "panel:Panel1", "high", "PC OFF",
                 fixable=False, now=0.0)
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    planned = incident_followup_step(
        tracker, 3600.0, followup_interval_s=900, giveup_s=3600, max_fix_retries=2)
    assert [a["kind"] for a in planned] == ["giveup"]
    snap_id = planned[0]["row"]["id"]

    tracker.resolve_by_bot("panel", "Panel1", "self_healed", now=3500.0)
    monkeypatch.setattr(mcp_watcher, "incident_followup_step", lambda *a, **k: planned)

    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", {"tracker": tracker}, 3600.0, deliver=True))

    assert tracker.get_open_by_id(snap_id) is None
    assert [m for m in msgs if "❌" in m] == []   # NO stale escalation alert
    tracker.close()


def test_followup_tick_refix_resolved_midawait_spares_reopened_row(tmp_path, monkeypatch):
    # The headline race: a refix presses buttons (await). If a monitor sweep
    # resolves the snapshotted incident DURING that await and reopens a fresh row
    # under the same key, the tick's mutation must target the OLD (now-resolved)
    # id and NO-OP — never pre-burning the fresh row's retry budget, never nagging.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _followup_cfg(tmp_path)
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=True, raw_excerpt="boom raw", now=0.0)
    old_id = tracker.open_for_bot("bot_error", "Bot1")["id"]
    client = _FakeClient()
    msgs = _capture_alerts(monkeypatch)

    # try_auto_fix simulates the mid-await monitor sweep: it resolves the open
    # incident AND reopens a fresh episode under the same key, then returns.
    async def racing_fix(client_, cfg_, bot, text, **k):
        tracker.resolve_by_bot("bot_error", bot, "self_healed", now=850.0)
        tracker.open("bot_error", bot, "bot_error:Bot1", "high", "boom again",
                     fixable=True, raw_excerpt="boom again", now=860.0)
        return {"status": "fixed"}
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", racing_fix)

    state = {"tracker": tracker, "watch": [("Bot1", object())]}
    asyncio.run(mcp_watcher._incident_followup_tick(
        client, cfg, tracker, "ibo", state, 900.0, deliver=True))

    fresh = tracker.open_for_bot("bot_error", "Bot1")
    assert fresh is not None
    assert fresh["id"] != old_id                 # the reopened episode
    assert fresh["fix_retries"] == 0             # budget NOT pre-burned by old id
    assert fresh["update_count"] == 0            # not nagged by the stale action
    # The old id is resolved and must stay so; note_fix_attempt/mark_followed_up
    # both no-op against it.
    assert tracker.get_open_by_id(old_id) is None
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


# ---------------------------------------------------------------------------
# Incident resolution attribution + keying (review regressions)
# ---------------------------------------------------------------------------

def test_resolve_reports_self_healed_after_failed_fix(tmp_path, monkeypatch):
    """An attempted-but-FAILED fix that then self-heals must report 'recovered on
    its own', NOT 'fixed by WatcherDog' — we_fixed is true only when a re-attempt
    actually reported success."""
    from watcherdog.incident_tracker import IncidentTracker
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    state = {"tracker": tracker}
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=True, now=100.0)
    tracker.note_fix_attempt("bot_error:Bot1", "failed")   # our retry FAILED

    sent = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        sent.append(text)
        return True

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    asyncio.run(mcp_watcher._resolve_incidents_for(
        state, _FakeClient(), "ibo", "Bot1", 280.0, True, _cfg(tmp_path)))

    assert len(sent) == 1
    assert "on its own" in sent[0]
    assert "WatcherDog" not in sent[0]
    assert tracker.open_for_bot("bot_error", "Bot1") is None
    tracker.close()


def test_resolve_reports_we_fixed_after_successful_refix(tmp_path, monkeypatch):
    """When a re-fix actually succeeded, the closure credits WatcherDog."""
    from watcherdog.incident_tracker import IncidentTracker
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    state = {"tracker": tracker}
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=True, now=100.0)
    tracker.note_fix_attempt("bot_error:Bot1", "fixed")    # our retry SUCCEEDED

    sent = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        sent.append(text)
        return True

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    asyncio.run(mcp_watcher._resolve_incidents_for(
        state, _FakeClient(), "ibo", "Bot1", 160.0, True, _cfg(tmp_path)))

    assert len(sent) == 1
    assert "WatcherDog" in sent[0]
    tracker.close()


def test_open_bot_incident_keyed_by_bot_avoids_leak(tmp_path):
    """Two distinct errors for one bot must produce ONE open incident (keyed by
    bot), so a later healthy message closes it instead of leaking an orphan that
    the follow-up loop would later falsely escalate."""
    from watcherdog.incident_tracker import IncidentTracker
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    state = {"tracker": tracker}
    mcp_watcher._open_bot_incident(state, "Bot1", "high",
                                   {"summary": "error A"}, "error A text",
                                   fixable=False, now=100.0)
    mcp_watcher._open_bot_incident(state, "Bot1", "high",
                                   {"summary": "error B"}, "error B text",
                                   fixable=False, now=130.0)
    assert len(tracker.open_list()) == 1
    tracker.close()


# ---------------------------------------------------------------------------
# Phase B Task 1: freshness gate exercised against the REAL IncidentTracker.
# The dict _FakeTracker (above) cannot catch the row.get() vs row["..."] class
# of bug because dicts have .get() but the real tracker returns sqlite3.Row
# (conn.row_factory = sqlite3.Row), which does NOT. These tests run the real
# tracker so the recovery path is genuinely exercised.
# ---------------------------------------------------------------------------

def test_fresh_normal_resolves_against_real_tracker(tmp_path):
    # A fresh normal message must resolve a real open bot_error incident WITHOUT
    # crashing. Guards against row.get() regressions on sqlite3.Row.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true"})
    store = IncidentStore(str(tmp_path / "store.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    now = time.time()
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=now - 600)   # opened 10 min ago
    client = _FakeClient()
    state = {"tracker": tracker}
    fresh = SimpleNamespace(timestamp=lambda: now)  # message is now (fresh)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$", now,
        asyncio.new_event_loop(), deliver=True, ent=None, date=fresh))

    assert tracker.open_for_bot("bot_error", "Bot1") is None  # resolved, no crash
    store.close()
    tracker.close()


def test_stale_normal_does_not_resolve_against_real_tracker(tmp_path):
    # A stale normal message (older than the open incident) must NOT resolve it,
    # and must not crash, against the real sqlite3.Row-backed tracker.
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true"})
    store = IncidentStore(str(tmp_path / "store.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    now = time.time()
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=now)        # opened just now (recent)
    client = _FakeClient()
    state = {"tracker": tracker}
    stale = SimpleNamespace(timestamp=lambda: now - 600)  # message 10 min old

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$", now,
        asyncio.new_event_loop(), deliver=True, ent=None, date=stale))

    assert tracker.open_for_bot("bot_error", "Bot1") is not None  # stays open
    store.close()
    tracker.close()


# ---------------------------------------------------------------------------
# _rearm_panel_episodes — re-arm panel latches from the ledger at startup
# ---------------------------------------------------------------------------

def test_rearm_panel_episodes_arms_open_panel_rows(tmp_path):
    # After a (re)start the in-memory panel latches are empty, but open panel:
    # rows persist in SQLite. _rearm_panel_episodes must re-arm coldcase_reported
    # for panel sources only — a bot_error row creates NO panel state — so the
    # followup loop doesn't falsely escalate a healed panel.
    from watcherdog.incident_tracker import IncidentTracker
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "SinFermera2", "panel:SinFermera2", "high",
                 "needs PC", fixable=False)
    tracker.open("bot_error", "Bot9", "bot_error:Bot9", "high",
                 "boom", fixable=False)

    mcp_watcher._rearm_panel_episodes({"tracker": tracker})

    assert mcp_watcher._PANEL_STATE["SinFermera2"].coldcase_reported is True
    assert "Bot9" not in mcp_watcher._PANEL_STATE  # bot_error makes no panel state
    tracker.close()


def test_rearm_panel_episodes_no_tracker_is_noop():
    # Tracking disabled / no tracker in state: a safe no-op, no exception, and
    # _PANEL_STATE stays empty (the autouse fixture cleared it).
    mcp_watcher._rearm_panel_episodes({})
    assert mcp_watcher._PANEL_STATE == {}


def test_sweep_continues_after_one_chat_raises(tmp_path, monkeypatch):
    # A raise inside the per-chat tail (_evaluate_bot / silence / healthy count)
    # must NOT abort the whole sweep: every chat after the failing one was being
    # skipped. The tail is now wrapped so one bad chat is logged and skipped.
    cfg = _cfg(tmp_path, {"PANEL_RULES_ENABLED": "false", "SILENCE_ENABLED": "false"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    seen = []

    async def fake_latest(client, ent, mark_read=False):
        return "some text", None

    async def fake_eval(client, cfg, store, state, target, bot, text, now, loop,
                        deliver=True, ent=None, date=None):
        if bot == "Bad":
            raise RuntimeError("boom in Bad")
        seen.append(bot)

    monkeypatch.setattr(mcp_watcher.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(mcp_watcher, "_evaluate_bot", fake_eval)

    watch = [("Bad", object()), ("Good", object())]
    asyncio.run(mcp_watcher.monitor_once(
        client, cfg, store, {}, watch, "ibo", deliver=True))
    assert seen == ["Good"]   # the failing chat didn't abort the sweep


def test_raising_chat_is_retried_next_sweep_not_memo_skipped(tmp_path, monkeypatch):
    # _evaluate_bot writes its unchanged-message memo (bot::last_eval_hash) BEFORE
    # the awaits that can raise. With the per-chat except now swallowing the raise,
    # a chat that DETERMINISTICALLY raises would set the memo, then on the next
    # sweep the memo matches → _evaluate_bot early-returns → the chat is SILENTLY
    # skipped forever. The except must clear the memo so the chat is re-attempted
    # (and re-logged) every sweep instead of going un-monitored invisibly.
    cfg = _cfg(tmp_path, {"PANEL_RULES_ENABLED": "false", "SILENCE_ENABLED": "false"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    calls = []

    async def fake_latest(client, ent, mark_read=False):
        return "boom text", None

    async def fake_eval(client, cfg, store, state, target, bot, text, now,
                        loop, deliver=True, ent=None, date=None):
        calls.append(bot)
        # mimic the real memo write before raising
        state[bot + "::last_eval_hash"] = "h"
        raise RuntimeError("always boom")

    monkeypatch.setattr(mcp_watcher.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(mcp_watcher, "_evaluate_bot", fake_eval)

    state = {}
    watch = [("Bad", object())]
    # two sweeps; the memo must be cleared after the first so the second RE-CALLS
    asyncio.run(mcp_watcher.monitor_once(client, cfg, store, state, watch, "ibo", deliver=True))
    asyncio.run(mcp_watcher.monitor_once(client, cfg, store, state, watch, "ibo", deliver=True))
    assert calls == ["Bad", "Bad"]                         # retried, not skipped
    assert "Bad::last_eval_hash" not in state              # memo cleared by the except


def test_monitor_once_refreshes_health_beacon(tmp_path, monkeypatch):
    # The health beacon must be a per-SWEEP heartbeat (not just a startup touch),
    # so the overseer health probe can tell a hung sweep loop from a healthy one.
    import os
    cfg = _cfg(tmp_path, {"DB_PATH": str(tmp_path / "incidents.db"),
                          "PANEL_RULES_ENABLED": "false", "SILENCE_ENABLED": "false"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()

    async def fake_latest(client, ent, mark_read=False):
        return "some text", None

    async def fake_eval(client, cfg, store, state, target, bot, text, now, loop,
                        deliver=True, ent=None, date=None):
        return None

    monkeypatch.setattr(mcp_watcher.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(mcp_watcher, "_evaluate_bot", fake_eval)

    assert not os.path.exists(cfg.watcher_health_path)        # nothing yet
    asyncio.run(mcp_watcher.monitor_once(
        client, cfg, store, {}, [("Good", object())], "ibo", deliver=True))
    assert os.path.exists(cfg.watcher_health_path)            # the sweep wrote it
