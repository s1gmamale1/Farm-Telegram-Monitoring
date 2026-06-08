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
                panel_max_attempts=3,
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
NON_STATUS = "🎁 [SinFermera7] collected drop · AK-47 | Redline - 0.27$"
OVERLAUNCH_ALERT = "[SinFermera24] All 8 accounts launched!"


@pytest.fixture(autouse=True)
def _clear_state():
    mcp_watcher._PANEL_STATE.clear()
    yield
    mcp_watcher._PANEL_STATE.clear()


def _run(cfg, text, *, age=1.0, name="SinFermera7", deliver=True, state=None, target=None):
    date = _date(time.time() - age)
    return asyncio.run(mcp_watcher._evaluate_panel(
        None, cfg, name, object(), text, date,
        deliver=deliver, state=state if state is not None else {}, target=target))


def test_healthy_returns_none():
    assert _run(_cfg(), HEALTHY) is None


def test_non_status_message_returns_none():
    # A normal non-status post (drop/match) must defer to the normal monitoring
    # path — engine returns None, never a "could not parse" flag.
    assert _run(_cfg(), NON_STATUS) is None


def test_cant_find_match_requests_screenshot_and_alerts_accounts(monkeypatch):
    alerts = []
    screenshots = []

    async def fake_menu(client, panel, *, timeout=20.0):
        return {"accounts": ["lilpro51", "nuggetgoat_irl8574"], "buttons": ["Screenshot"]}

    async def fake_screenshot(client, panel, *, cfg=None, timeout=30.0):
        screenshots.append(panel)
        return {"downloaded": "/tmp/shot.png", "caption": "screen"}

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)
        return True

    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    monkeypatch.setattr(mcp_watcher.tg_actions, "screenshot", fake_screenshot)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)

    note = _run(
        _cfg(),
        "[SinFermera4] Can't find match in 70 minutes. Changing batch...",
        name="SinFermera4",
        target="ibo",
    )

    assert note == "match-search issue flagged"
    assert screenshots
    assert alerts
    assert "Can't find match" in alerts[0]
    assert "lilpro51" in alerts[0]
    assert "nuggetgoat_irl8574" in alerts[0]
    assert "/tmp/shot.png" in alerts[0]


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


def test_stale_label_reports_silence_duration(monkeypatch):
    # The R6 "down" report must say HOW LONG it's been silent, not just a vague
    # "panel/PC down". (cfg stale=30m here; a 70m-old message is dead.)
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    _run(_cfg(), HEALTHY, age=4200)   # 70 min old > 30m stale → dead
    assert alerts and "silent" in alerts[0].lower() and "needs PC" in alerts[0]
    assert "70m" in alerts[0]


def test_flag_alert_latched_once(monkeypatch):
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    cfg = _cfg()
    n1 = _run(cfg, HEALTHY, age=3600)
    n2 = _run(cfg, HEALTHY, age=3600)   # still stale next sweep
    assert n1 is not None and n2 is not None      # both handled (AI path skipped)
    assert len(alerts) == 1                        # but alerted only ONCE


def test_flag_latch_clears_on_recovery(monkeypatch):
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    cfg = _cfg()
    _run(cfg, HEALTHY, age=3600)        # stale -> alert #1
    assert _run(cfg, HEALTHY, age=1) is None   # recovered -> noop, clears latch
    _run(cfg, HEALTHY, age=3600)        # stale again -> alert #2
    assert len(alerts) == 2


def test_overlaunch_alert_wired_to_recovery(monkeypatch):
    cards = []

    async def fake_card(state, title, options, *, panel_target):
        cards.append(title)
        return 123

    async def fake_seq(*a, **k):
        raise AssertionError("destructive over-launch fix must offer a card, not auto-run")

    monkeypatch.setattr(mcp_watcher, "_offer_card", fake_card)
    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    # "All 8 accounts launched!" is not a status card; launched_from_alert must
    # feed 8 in so it becomes an over-launch (R1) instead of a parse-failure noop.
    note = _run(_cfg(panel_overlaunch_minutes=0), OVERLAUNCH_ALERT, name="SinFermera24")
    assert note is not None and "confirm card" in note
    assert cards and "SinFermera24" in cards[0]


def test_overlaunch_destructive_offers_confirm_card(monkeypatch):
    cards = []

    async def fake_card(state, title, options, *, panel_target):
        cards.append(title)
        return 123

    async def fake_seq(*a, **k):
        raise AssertionError("destructive action must not auto-run; should offer a card")

    monkeypatch.setattr(mcp_watcher, "_offer_card", fake_card)
    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    note = _run(_cfg(panel_overlaunch_minutes=0), OVER)
    assert note is not None and "confirm card" in note
    assert cards and "SinFermera7" in cards[0]


def test_debounce_blocks_repeat(monkeypatch):
    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    cfg = _cfg(panel_action_debounce_seconds=9999)
    n1 = _run(cfg, UNDER)
    n2 = _run(cfg, UNDER)
    assert n1 is not None and "-> ok" in n1
    assert n2 is None


def test_r4_black_screenshot_flags_after_failed_relaunch(monkeypatch):
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

    _run(cfg, UNDER)        # 1st sweep: R2 relaunch runs, arms r2_attempted_ts
    n2 = _run(cfg, UNDER)   # 2nd sweep: still under + black -> R4 cold case
    assert ran == [["select_unfarmed", "start_selected"]]
    assert n2 == "R4 cold-case flagged"
    assert alerts and ("black" in alerts[0].lower() or "rdp" in alerts[0].lower())


def test_destructive_auto_runs_when_enabled(monkeypatch):
    # Owner chose "auto-fix all": with PANEL_AUTO_DESTRUCTIVE on, the Kill-all
    # over-launch fix executes autonomously instead of offering a confirm card.
    ran = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    async def fake_card(*a, **k):
        raise AssertionError("auto-destructive must NOT offer a card")

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher, "_offer_card", fake_card)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    note = _run(_cfg(panel_overlaunch_minutes=0, panel_auto_destructive=True), OVER)
    assert ran == [["kill_all", "select_unfarmed", "start_selected"]]
    assert "-> ok" in note


def test_fixed_report_on_recovery(monkeypatch):
    # After a recovery attempt, when the panel returns healthy, emit ONE line.
    alerts = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        return [{"ok": True} for _ in actions]

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    cfg = _cfg()
    _run(cfg, UNDER)                       # attempt R2 (2/4 launched)
    assert _run(cfg, HEALTHY) is None      # recovered -> Fixed report + reset
    assert len(alerts) == 1
    assert alerts[0] == "SinFermera7 | 2/4 launched | Fixed ✅"


def test_cold_case_after_max_attempts(monkeypatch):
    # A frozen-PC loop: after panel_max_attempts failed relaunches, stop the
    # futile loop and escalate ONCE as a cold case, then stay quiet.
    alerts = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        return [{"ok": True} for _ in actions]

    async def fake_black(client, panel, cfg):
        return {"black": False}            # not black -> let attempts climb to the cap

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.panel_actions, "screenshot_black", fake_black)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.daily_report, "record", lambda *a, **k: None)
    cfg = _cfg(panel_action_debounce_seconds=0, panel_max_attempts=2)
    _run(cfg, UNDER)                       # attempt 1
    _run(cfg, UNDER)                       # attempt 2
    n3 = _run(cfg, UNDER)                  # cap hit -> cold-case escalation
    n4 = _run(cfg, UNDER)                  # already escalated -> quiet
    assert n3 == "cold-case: attempts exhausted"
    assert n4 == "cold-case: awaiting PC"
    assert len(alerts) == 1
    assert "NOT fixed" in alerts[0] and "needs PC" in alerts[0]
    assert "SinFermera7 | 2/4 launched" in alerts[0]
