"""Launch-grace + RDP-bug auto-reboot (owner-authorized, from the live SF21
episode 2026-06-12). Tests drive the mcp_watcher helpers directly with fakes
for the Telegram layer; time is injected."""

from __future__ import annotations

import asyncio
import types

from watcherdog import mcp_watcher, panel_rules


def _cfg(**kw):
    base = dict(panel_launch_grace_minutes=15, rdp_bug_reboot_minutes=30,
                reboot_wait_minutes=15, panel_auto_destructive=True,
                panel_max_attempts=3, panel_action_debounce_seconds=180,
                panel_stale_minutes=70, panel_probe_timeout=1.0,
                panel_target_accounts=4, panel_auto_recover=True,
                daily_errors_path=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(coro):
    return asyncio.run(coro)


# --- _note_rdp_bug -------------------------------------------------------------

def test_rdp_marker_arms_once_and_never_refreshes():
    ps = panel_rules.PanelState()
    assert mcp_watcher._note_rdp_bug(ps, "➔ ❌ Error creating screenshot: screen grab failed", 100.0)
    assert ps.rdp_bug_since == 100.0
    mcp_watcher._note_rdp_bug(ps, "screen grab failed again", 500.0)
    assert ps.rdp_bug_since == 100.0          # measures "bugged since", no refresh
    assert not mcp_watcher._note_rdp_bug(ps, "[SF7] warmup started", 600.0)


# --- _maybe_reboot_for_rdp_bug ---------------------------------------------------

def _reboot_env(monkeypatch, *, confirmed=True):
    calls = {"reboot": 0, "alert": []}

    async def fake_reboot(client, panel, cfg):
        calls["reboot"] += 1
        return {"pressed": "🔄⚠️ Reboot PC", "confirmed": confirmed, "result": "ok"}

    async def fake_alert(state, client, target, text, deliver, cfg=None):
        calls["alert"].append(text)
        return True

    monkeypatch.setattr(mcp_watcher.panel_actions, "reboot_pc", fake_reboot)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    return calls


def test_reboot_fires_past_threshold(monkeypatch):
    calls = _reboot_env(monkeypatch)
    ps = panel_rules.PanelState(rdp_bug_since=0.0)
    now = 31 * 60.0
    note = _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True, now,
        about_to_coldcase=True))
    assert note == "rdp-bug reboot pressed"
    assert calls["reboot"] == 1
    assert ps.reboot_attempted is True and ps.reboot_ts == now
    assert any("Reboot PC" in a for a in calls["alert"])


def test_reboot_holds_coldcase_below_threshold(monkeypatch):
    calls = _reboot_env(monkeypatch)
    ps = panel_rules.PanelState(rdp_bug_since=0.0)
    note = _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True, 10 * 60.0,
        about_to_coldcase=True))
    assert note is not None and "hold" in note     # cold case superseded
    assert calls["reboot"] == 0
    # but a mere relaunch sequence below threshold is NOT held:
    note2 = _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True, 10 * 60.0,
        about_to_coldcase=False))
    assert note2 is None


def test_reboot_once_per_episode_and_gates(monkeypatch):
    calls = _reboot_env(monkeypatch)
    now = 31 * 60.0
    # already attempted -> falls through (cold case proceeds)
    ps = panel_rules.PanelState(rdp_bug_since=0.0, reboot_attempted=True)
    assert _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True, now,
        about_to_coldcase=True)) is None
    # dry-run -> never presses
    ps2 = panel_rules.PanelState(rdp_bug_since=0.0)
    assert _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps2, {}, "ibo", False, now,
        about_to_coldcase=True)) is None
    # auto-destructive off -> never presses
    ps3 = panel_rules.PanelState(rdp_bug_since=0.0)
    assert _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(panel_auto_destructive=False), "SF21", object(), ps3, {},
        "ibo", True, now, about_to_coldcase=True)) is None
    assert calls["reboot"] == 0
    # no rdp signal -> None
    ps4 = panel_rules.PanelState()
    assert _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps4, {}, "ibo", True, now,
        about_to_coldcase=True)) is None


def test_post_reboot_quiet_window_then_expiry(monkeypatch):
    _reboot_env(monkeypatch)
    ps = panel_rules.PanelState(rdp_bug_since=0.0, reboot_attempted=True,
                                reboot_ts=1000.0)
    within = _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True,
        1000.0 + 5 * 60, about_to_coldcase=False))
    assert within == "post-reboot quiet wait"
    after = _run(mcp_watcher._maybe_reboot_for_rdp_bug(
        object(), _cfg(), "SF21", object(), ps, {}, "ibo", True,
        1000.0 + 16 * 60, about_to_coldcase=True))
    assert after is None                       # window over: caller may cold-case


# --- the SF21 regression replay (self-report handler, real timeline) -----------

def test_sf21_replay_no_attempts_burned_mid_launch(monkeypatch):
    """The real 2026-06-12 episode: probe replies say 'Accounts launching...'
    at T+0/T+4/T+8 — the OLD code burned 3 attempts and cold-cased at T+8;
    the fix must burn ZERO attempts and never cold-case mid-launch."""
    mcp_watcher._PANEL_STATE.clear()
    probe_text = (
        "⌨️ FSM Panel - Main menu ⌨️\n"
        "➔ ❌ Error creating screenshot: screen grab failed\n"
        "📋 Panel status:\n"
        "├ 👥 Launched: 1 accounts\n"
        "└ 🚀 Status: Accounts launching...\n"
    )

    async def fake_probe(client, target_ref, cfg):
        return True, probe_text

    async def fail_seq(*a, **k):
        raise AssertionError("BUG: relaunch pressed mid-launch")

    monkeypatch.setattr(mcp_watcher, "_panel_probe", fake_probe)
    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fail_seq)
    cfg = _cfg()
    state = {}
    for _ in range(3):    # three sweeps inside the grace window
        note = _run(mcp_watcher._handle_panel_selfreport_silence(
            object(), cfg, "SinFermera21", object(), deliver=True, state=state,
            target="ibo", ent=None, seed=False))
        assert "launch in progress" in note
    ps = mcp_watcher._PANEL_STATE["SinFermera21"]
    assert ps.recover_attempts == 0            # zero attempts burned
    assert ps.coldcase_reported is False       # no cold case mid-launch
    assert ps.rdp_bug_since is not None        # the ➔ error line armed the signal
