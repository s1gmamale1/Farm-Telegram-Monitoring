"""Tests for watcherdog.novel_recovery — the Phase 4 generic-restart ladder."""

from __future__ import annotations

import asyncio
import types

from watcherdog import novel_recovery


def _cfg(**kw):
    base = dict(novel_recovery=True, agent_actions_enabled=True,
                daily_errors_path=None, panel_settle_seconds=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(coro):
    return asyncio.run(coro)


def test_critical_family_is_human_needed_and_never_presses(monkeypatch):
    called = []

    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}] * 3

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] account banned by VAC",
                                      chat=object(), deliver=True))
    assert out["status"] == "human_needed"
    assert called == []                      # the gate runs FIRST — no press


def test_flag_off_dryrun_and_actions_off_all_skip(monkeypatch):
    called = []

    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}] * 3

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    novel_text = "[SinFermera7] flux capacitor desync"   # not critical-family
    for cfg, deliver in ((_cfg(novel_recovery=False), True),
                         (_cfg(), False),
                         (_cfg(agent_actions_enabled=False), True)):
        out = _run(novel_recovery.attempt(object(), cfg, "SF7", novel_text,
                                          chat=object(), deliver=deliver))
        assert out["status"] == "skipped"
    assert called == []


def test_happy_ladder_attempted_in_order(monkeypatch):
    seen = {}

    async def fake_seq(client, panel, actions, cfg, *, confirmed=False):
        seen["actions"] = list(actions)
        seen["confirmed"] = confirmed
        return [{"ok": True}] * len(actions)

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "attempted"
    assert seen["actions"] == ["kill_all", "select_unfarmed", "start_selected"]
    assert seen["confirmed"] is True


def test_mid_ladder_failure_reports_step(monkeypatch):
    async def fake_seq(client, panel, actions, cfg, *, confirmed=False):
        return [{"ok": True}, {"ok": False, "detail": {"error": "timeout"}}]

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "failed"
    assert out["failed_step"] == "select_unfarmed"


def test_attempt_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("telethon died")

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", boom)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "failed"


# --- alerter.format_novel_alert ----------------------------------------------

from watcherdog.alerter import format_novel_alert


def test_format_novel_alert_attempted_line():
    out = format_novel_alert("SF7", "high", {"summary": "weird"}, "raw",
                             {"status": "attempted"})
    assert "generic restart" in out and "verify next sweep" in out


def test_format_novel_alert_failed_names_step():
    out = format_novel_alert("SF7", "high", {"summary": "weird"}, "raw",
                             {"status": "failed", "failed_step": "kill_all"})
    assert "FAILED at 'kill_all'" in out


def test_format_novel_alert_human_needed_and_skipped():
    human = format_novel_alert("SF7", "critical", {}, "banned", {"status": "human_needed"})
    assert "not auto-restarting" in human
    plain = format_novel_alert("SF7", "high", {}, "raw", {"status": "skipped"})
    assert "🛠" not in plain and "🚫" not in plain      # plain alert, no recovery line


# --- mcp_watcher wiring -------------------------------------------------------

def test_open_bot_incident_passes_novel_flag(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    state = {"tracker": t}
    mcp_watcher._open_bot_incident(state, "SF7", "high", {"summary": "weird"},
                                   "raw text", fixable=True, novel=True, now=100.0)
    row = t.open_for_bot("bot_error", "SF7")
    assert row["novel"] == 1 and row["fixable"] == 1
    t.close()


def test_refix_tick_routes_novel_to_ladder(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird", fixable=True,
           novel=True, raw_excerpt="weird novel text", now=0.0)
    t.open("bot_error", "SF8", "bot_error:SF8", "high", "known", fixable=True,
           raw_excerpt="known text", now=0.0)
    calls = {"novel": [], "auto": []}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        calls["novel"].append(bot)
        return {"status": "attempted"}

    async def fake_autofix(client, cfg, bot, text, *, chat=None):
        calls["auto"].append(bot)
        return {"status": "failed"}

    async def fake_alert(*a, **k):
        return True

    monkeypatch.setattr(mcp_watcher.novel_recovery, "attempt", fake_attempt)
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", fake_autofix)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher, "_entity_for", lambda state, bot: object())

    cfg = types.SimpleNamespace(incident_followup_interval=10,
                                incident_giveup_seconds=10_000,
                                incident_max_fix_retries=3)
    asyncio.run(mcp_watcher._incident_followup_tick(
        object(), cfg, t, "target", {}, now=1000.0, deliver=True))
    assert calls["novel"] == ["SF7"]
    assert calls["auto"] == ["SF8"]
    t.close()


def test_transient_strong_error_still_ladders(monkeypatch):
    """Design fidelity: crash/stuck/timeout novel errors are EXACTLY what the
    restart ladder is for — only the account-level family (ban/captcha/guard)
    is exempt. Regression for the over-broad severity_of gate."""
    seen = {}

    async def fake_seq(client, panel, actions, cfg, *, confirmed=False):
        seen["actions"] = list(actions)
        return [{"ok": True}] * len(actions)

    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] CS2 crash detected, restart timed out",
                                      chat=object(), deliver=True))
    assert out["status"] == "attempted"
    assert seen["actions"] == ["kill_all", "select_unfarmed", "start_selected"]


# --- _evaluate_bot novel-branch wiring (real tracker + store) -----------------

import time as _time

from watcherdog.config import Config
from watcherdog.storage import IncidentStore


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, text))


def _wiring_env(tmp_path, monkeypatch, attempt_status):
    """Real Config/IncidentStore/IncidentTracker; auto_fix finds nothing;
    novel_recovery.attempt returns the given status."""
    from watcherdog import mcp_watcher
    from watcherdog.incident_tracker import IncidentTracker
    cfg = Config({"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "true",
                  "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0",
                  "DAILY_ERRORS_PATH": str(tmp_path / "daily.jsonl")})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "open.db"))

    async def no_fix(*_a, **_k):
        return None

    async def fake_attempt(client, c, bot, text, *, chat=None, deliver=True):
        return {"status": attempt_status}

    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", no_fix)
    monkeypatch.setattr(mcp_watcher.novel_recovery, "attempt", fake_attempt)
    monkeypatch.setattr(mcp_watcher.learned_fixes, "find_fix", lambda *a, **k: None)
    return mcp_watcher, cfg, store, tracker


def test_evaluate_bot_novel_branch_opens_flagged_incident(tmp_path, monkeypatch):
    mcp_watcher, cfg, store, tracker = _wiring_env(tmp_path, monkeypatch, "attempted")
    client = _FakeClient()
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {"tracker": tracker}, "ibo", "SinFermera9",
        "Got an error while launching accounts.", _time.time(),
        asyncio.new_event_loop(), deliver=True, ent=None))
    row = tracker.open_for_bot("bot_error", "SinFermera9")
    assert row is not None
    assert row["novel"] == 1                  # flagged for the overseer queue
    assert row["fixable"] == 1                # ladder ran -> refix loop retries
    assert row["fix_retries"] == 1            # inline attempt burned budget #1
    assert client.sent                        # owner alerted
    store.close(); tracker.close()


def test_evaluate_bot_human_needed_no_budget_burn(tmp_path, monkeypatch):
    mcp_watcher, cfg, store, tracker = _wiring_env(tmp_path, monkeypatch, "human_needed")
    client = _FakeClient()
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {"tracker": tracker}, "ibo", "SinFermera9",
        "Got an error while launching accounts.", _time.time(),
        asyncio.new_event_loop(), deliver=True, ent=None))
    row = tracker.open_for_bot("bot_error", "SinFermera9")
    assert row["novel"] == 1
    assert row["fixable"] == 0                # never refix a human-needed error
    assert row["fix_retries"] == 0            # no attempt burned
    store.close(); tracker.close()


def test_evaluate_bot_known_fix_not_flagged_novel(tmp_path, monkeypatch):
    """Actions-off mislabel regression: an error WITH a learned fix must not be
    flagged novel even when try_auto_fix produced no outcome."""
    mcp_watcher, cfg, store, tracker = _wiring_env(tmp_path, monkeypatch, "attempted")
    monkeypatch.setattr(mcp_watcher.learned_fixes, "find_fix",
                        lambda *a, **k: {"signature": "known thing"})
    client = _FakeClient()
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {"tracker": tracker}, "ibo", "SinFermera9",
        "Got an error while launching accounts.", _time.time(),
        asyncio.new_event_loop(), deliver=True, ent=None))
    row = tracker.open_for_bot("bot_error", "SinFermera9")
    assert row is not None
    assert row["novel"] == 0                  # known error: plain alert path
    assert row["fix_retries"] == 0
    store.close(); tracker.close()


def test_panel_cold_case_enters_overseer_queue(tmp_path):
    """Phase 6: a cold-cased panel (text exhausted — only vision can diagnose)
    must appear in novel_list(), the overseer's list_flagged queue."""
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    mcp_watcher._open_panel_incident({"tracker": t}, "SinFermera13",
                                     "panel/PC down — needs PC", now=100.0)
    queue = t.novel_list()
    assert [r["bot"] for r in queue] == ["SinFermera13"]
    assert queue[0]["source"] == "panel"
    assert queue[0]["fixable"] == 0          # followup nag baseline unchanged
    t.close()
