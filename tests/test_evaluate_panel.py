"""Behavioral tests for mcp_watcher._evaluate_panel — the deterministic panel
orchestration (R1-R6) that ties farm_stats + panel_rules + panel_actions
together. Dependencies (Telegram read, alert, card, sequence run) are mocked at
the boundary; no network, no model."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from watcherdog import mcp_watcher


def _cfg(**kw):
    base = dict(panel_rules_enabled=True, panel_target_accounts=4,
                panel_overlaunch_minutes=15, panel_idle_minutes=10,
                panel_stale_minutes=30, panel_action_debounce_seconds=180,
                panel_auto_recover=True, panel_auto_destructive=False,
                panel_settle_seconds=0, daily_errors_path="/tmp/wd_test_panel.jsonl")
    base.update(kw)
    return SimpleNamespace(**base)


def _date(ts):
    return SimpleNamespace(timestamp=lambda: ts)


HEALTHY = ("📊 Panel status:\n├ 👥 Launched: 4 accounts\n├ 🟢 Status: LIVE\n"
           "├ Map: de_nuke\n└ Score: [1:0]")
UNDER = "📊 Panel status:\n├ 👥 Launched: 2 accounts\n├ 🟢 Status: LIVE"
OVER = ("📊 Panel status:\n├ 👥 Launched: 8 accounts\n├ 🟢 Status: LIVE\n"
        "├ Map: m\n└ Score: [1:0]")


@pytest.fixture(autouse=True)
def _clear_state():
    mcp_watcher._PANEL_STATE.clear()
    yield
    mcp_watcher._PANEL_STATE.clear()


def _patch_latest(monkeypatch, text, age=1.0):
    async def fake(client, ent, mark_read=False):
        return text, _date(time.time() - age)
    monkeypatch.setattr(mcp_watcher.tg_tools, "latest_message", fake)


def _run(cfg, name="SinFermera7", deliver=True, state=None):
    return asyncio.run(mcp_watcher._evaluate_panel(
        None, cfg, name, object(), deliver=deliver, state=state if state is not None else {}))


def test_healthy_returns_none(monkeypatch):
    _patch_latest(monkeypatch, HEALTHY)
    assert _run(_cfg()) is None


def test_dry_run_does_not_press(monkeypatch):
    _patch_latest(monkeypatch, UNDER)
    called = []

    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    note = _run(_cfg(), deliver=False)
    assert note.startswith("dry-run:") and called == []


def test_under_launch_auto_runs_sequence(monkeypatch):
    _patch_latest(monkeypatch, UNDER)
    ran = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    note = _run(_cfg())
    assert ran == [["select_unfarmed", "start_selected"]]
    assert "-> ok" in note


def test_stale_flags_cold_case(monkeypatch):
    _patch_latest(monkeypatch, HEALTHY, age=3600)   # 1h old > 30m stale
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    note = _run(_cfg())
    assert note is not None and ("down" in note.lower() or "stale" in note.lower())
    assert alerts and "SinFermera7" in alerts[0]


def test_overlaunch_destructive_offers_confirm_card(monkeypatch):
    _patch_latest(monkeypatch, OVER)
    cards = []

    async def fake_card(state, title, options, *, panel_target):
        cards.append(title)
        return 123   # truthy -> card posted

    async def fake_seq(*a, **k):
        raise AssertionError("destructive action must not auto-run; should offer a card")

    monkeypatch.setattr(mcp_watcher, "_offer_card", fake_card)
    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    note = _run(_cfg(panel_overlaunch_minutes=0))   # fire on first observation
    assert note is not None and "confirm card" in note
    assert cards and "SinFermera7" in cards[0]
