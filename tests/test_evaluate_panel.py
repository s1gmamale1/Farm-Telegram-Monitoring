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
SEARCHING = ("📊 Panel status:\n├ 👥 Launched: 4 accounts\n"
             "└ ⏳ Status: Searching game...")


@pytest.fixture(autouse=True)
def _clear_state():
    mcp_watcher._PANEL_STATE.clear()
    yield
    mcp_watcher._PANEL_STATE.clear()


def _run(cfg, text, *, age=1.0, name="SinFermera7", deliver=True, state=None,
         target=None, seed=False):
    date = _date(time.time() - age)
    return asyncio.run(mcp_watcher._evaluate_panel(
        None, cfg, name, object(), text, date,
        deliver=deliver, state=state if state is not None else {}, target=target,
        seed=seed))


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


def test_searching_is_healthy_returns_none():
    # A panel actively "Searching game..." with 4 launched is working — the engine
    # must not relaunch it. (Regression: "Searching" was misread as not-LIVE → R2.)
    assert _run(_cfg(), SEARCHING) is None


def test_silent_panel_that_answers_start_is_alive(monkeypatch):
    # R6: before declaring a silent panel dead, /start it. A reply proves it's
    # alive — no "dead" alert (the SinFermera19 false-positive).
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        return {"buttons": ["Screenshot"], "accounts": []}   # replied -> alive

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    note = _run(_cfg(), HEALTHY, age=4200)     # 70m old > 30m stale
    assert note == "probe: alive"
    assert alerts == []                         # NOT reported dead


def test_silent_panel_no_reply_is_dead(monkeypatch):
    # No reply to /start within the timeout -> genuinely unreachable -> needs PC.
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        return {"error": "no /start menu reply", "buttons": []}

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    note = _run(_cfg(), HEALTHY, age=4200)
    assert note is not None
    assert alerts and "silent" in alerts[0].lower() and "needs PC" in alerts[0]


def test_pc_off_no_start_reply_is_high_alert(monkeypatch):
    # /start gets NO reply -> the FSM Panel app on that PC is unreachable (PC off /
    # crashed). Nothing automated can fix a powered-off machine, so this is the
    # HIGH-priority, human-only "power on the PC" case (distinct from R4 black
    # screen, where the PC is on and the per-PC tool retries).
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        return {"error": "no /start menu reply", "buttons": []}

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    _run(_cfg(), HEALTHY, age=4200)
    assert alerts
    a = alerts[0]
    assert "HIGH" in a and "PC OFF" in a.upper() and "power on" in a.lower()
    assert "70m" in a                          # still says how long it's been silent


def test_probe_disabled_falls_back_to_timing_dead(monkeypatch):
    # With probing OFF we can't confirm PC-off, so keep the timing-only "dead"
    # report (no HIGH "PC OFF" claim) and never call /start.
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        raise AssertionError("must not probe when PANEL_PROBE_ENABLED is false")

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    _run(_cfg(panel_probe_enabled=False), HEALTHY, age=4200)
    assert alerts and "silent" in alerts[0].lower() and "HIGH" not in alerts[0]


def test_first_sweep_seeds_silence_without_alert_or_probe(monkeypatch):
    # On the FIRST sweep after (re)start, an already-quiet panel is SEEDED quietly:
    # no probe, no alert — so a restart never floods (the 11:11 burst).
    alerts, probed = [], []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        probed.append(panel)
        return {"error": "x", "buttons": []}

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    note = _run(_cfg(), HEALTHY, age=4200, seed=True)
    assert alerts == [] and probed == []        # neither alerted nor probed
    assert note is not None                       # but handled (AI path skipped)


async def _menu_no_reply(client, panel, *, timeout=20.0):
    # Probe runs but the panel never answers /start -> confirmed PC off.
    return {"error": "no /start menu reply", "buttons": []}


def test_stale_flags_cold_case(monkeypatch):
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", _menu_no_reply)
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
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", _menu_no_reply)
    _run(_cfg(), HEALTHY, age=4200)   # 70 min old > 30m stale → dead
    assert alerts and "silent" in alerts[0].lower() and "needs PC" in alerts[0]
    assert "70m" in alerts[0]


def test_flag_alert_latched_once(monkeypatch):
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", _menu_no_reply)
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
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", _menu_no_reply)
    cfg = _cfg()
    _run(cfg, HEALTHY, age=3600)        # stale + no /start -> PC OFF alert #1
    assert _run(cfg, HEALTHY, age=1) is None   # recovered -> back-online #2, clears latch
    _run(cfg, HEALTHY, age=3600)        # stale again -> PC OFF alert #3
    assert len(alerts) == 3
    assert "PC OFF" in alerts[0].upper()
    assert "back online" in alerts[1].lower()
    assert "PC OFF" in alerts[2].upper()


def test_probe_inconclusive_does_not_alert(monkeypatch):
    # The probe ITSELF failing (watcher-side network/FloodWait) is inconclusive —
    # it must NOT escalate a false "PC OFF" alert; it retries on a later sweep.
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)

    async def fake_menu(client, panel, *, timeout=20.0):
        raise RuntimeError("FloodWaitError / network blip on the watcher side")

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    note = _run(_cfg(), HEALTHY, age=4200)
    assert note == "probe: inconclusive"
    assert alerts == []        # NO false PC-OFF alert on a watcher-side error


def test_alive_probe_is_debounced(monkeypatch):
    # An alive-but-idle panel must be /start-probed at most once per debounce
    # window, not every sweep.
    probes = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        pass

    async def fake_menu(client, panel, *, timeout=20.0):
        probes.append(panel)
        return {"buttons": ["Screenshot"], "accounts": []}   # alive

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher.tg_actions, "panel_menu", fake_menu)
    cfg = _cfg()   # debounce 180s
    n1 = _run(cfg, HEALTHY, age=4200)
    n2 = _run(cfg, HEALTHY, age=4200)   # within debounce window
    assert n1 == "probe: alive"
    assert n2 == "probe: debounced"
    assert len(probes) == 1             # probed only once


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
