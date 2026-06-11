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
