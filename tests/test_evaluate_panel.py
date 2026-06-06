"""Behavioral tests for mcp_watcher._evaluate_panel — the deterministic panel
orchestration (R1-R6) that ties farm_stats + panel_rules + panel_actions
together. The status read is passed in (no Telegram); side effects (alert, card,
sequence run, screenshot) are mocked at the boundary. No network, no model."""

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


def _run(cfg, text, *, age=1.0, name="SinFermera7", deliver=True, state=None):
    date = _date(time.time() - age)
    return asyncio.run(mcp_watcher._evaluate_panel(
        None, cfg, name, object(), text, date,
        deliver=deliver, state=state if state is not None else {}))


def test_healthy_returns_none():
    assert _run(_cfg(), HEALTHY) is None


def test_dry_run_does_not_press(monkeypatch):
    called = []

    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    note = _run(_cfg(), UNDER, deliver=False)
    assert note.startswith("dry-run:") and called == []


def test_under_launch_auto_runs_sequence(monkeypatch):
    ran = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    note = _run(_cfg(), UNDER)
    assert ran == [["select_unfarmed", "start_selected"]]
    assert "-> ok" in note


def test_stale_flags_cold_case(monkeypatch):
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    note = _run(_cfg(), HEALTHY, age=3600)   # 1h old > 30m stale
    assert note is not None and ("down" in note.lower() or "stale" in note.lower())
    assert alerts and "SinFermera7" in alerts[0]


def test_overlaunch_destructive_offers_confirm_card(monkeypatch):
    cards = []

    async def fake_card(state, title, options, *, panel_target):
        cards.append(title)
        return 123   # truthy -> card posted

    async def fake_seq(*a, **k):
        raise AssertionError("destructive action must not auto-run; should offer a card")

    monkeypatch.setattr(mcp_watcher, "_offer_card", fake_card)
    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    note = _run(_cfg(panel_overlaunch_minutes=0), OVER)   # fire on first observation
    assert note is not None and "confirm card" in note
    assert cards and "SinFermera7" in cards[0]


def test_debounce_blocks_repeat(monkeypatch):
    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    cfg = _cfg(panel_action_debounce_seconds=9999)
    n1 = _run(cfg, UNDER)            # runs, sets last_action_ts
    n2 = _run(cfg, UNDER)            # within debounce window -> skipped
    assert n1 is not None and "-> ok" in n1
    assert n2 is None


def test_r4_black_screenshot_flags_after_failed_relaunch(monkeypatch):
    # debounce=0 so the R4 follow-up arms on the second sweep immediately.
    cfg = _cfg(panel_action_debounce_seconds=0)
    ran, alerts = [], []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    async def fake_black(client, panel, cfg):
        return {"black": True}

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.panel_actions, "screenshot_black", fake_black)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)

    n1 = _run(cfg, UNDER)   # 1st sweep: R2 relaunch runs, arms r2_attempted_ts
    n2 = _run(cfg, UNDER)   # 2nd sweep: still under-launched + black -> R4 cold case
    assert ran == [["select_unfarmed", "start_selected"]]    # R2 ran once, not twice
    assert n2 == "R4 cold-case flagged"
    assert alerts and ("black" in alerts[0].lower() or "rdp" in alerts[0].lower())
