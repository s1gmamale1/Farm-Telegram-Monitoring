from types import SimpleNamespace
from watcherdog import panel_rules as pr
from watcherdog.farm_stats import PanelStatus

CFG = SimpleNamespace(panel_target_accounts=4, panel_overlaunch_minutes=15,
                      panel_idle_minutes=10, panel_stale_minutes=30,
                      panel_action_debounce_seconds=180)


def _status(**kw):
    return PanelStatus(**kw)


def test_healthy_is_noop():
    s = _status(launched=4, status="LIVE", map="de_nuke", score="[1:0]", in_match=True)
    st = pr.observe(s, pr.PanelState(), 1000.0, CFG)
    assert pr.decide(s, 5.0, st, 1000.0, CFG).kind == "noop"


def test_r6_stale_flags_cold_case():
    s = _status(launched=4, status="LIVE", in_match=True)
    d = pr.decide(s, 60 * 60, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "flag" and d.cold_case is True


def test_r6_unreadable_flags_cold_case():
    d = pr.decide(None, None, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "flag" and d.cold_case is True


def test_r1_overlaunch_waits_then_acts_destructive():
    s = _status(launched=8, status="LIVE", in_match=True)
    st = pr.PanelState()
    st = pr.observe(s, st, 0.0, CFG)
    assert pr.decide(s, 5.0, st, 60.0, CFG).kind == "noop"
    d = pr.decide(s, 5.0, st, 16 * 60.0, CFG)
    assert d.kind == "sequence" and d.destructive is True
    assert d.actions == ["kill_all", "select_unfarmed", "start_selected"]


def test_r2_underlaunch_restores_four():
    s = _status(launched=2, status="LIVE")
    d = pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "sequence" and d.destructive is False
    assert d.actions == ["select_unfarmed", "start_selected"]


def test_r2_not_live_restores():
    s = _status(launched=4, status="OFFLINE")
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_r3_idle_no_match_makes_lobbies():
    s = _status(launched=4, status="LIVE", in_match=False)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["make_lobbies"]


def test_searching_game_is_healthy_noop():
    # "Status: Searching game..." with 4 launched is the healthy WORKING state
    # (accounts up, actively looking for a match). It must NOT relaunch (R2) nor
    # press make_lobbies (R3) — it's a noop.
    s = _status(launched=4, status="Searching game...", in_match=False)
    d = pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG)
    assert d.kind == "noop", f"expected healthy noop, got {d.kind}/{d.actions}"


def test_searching_under_target_still_relaunches():
    # Operational status does NOT excuse a low account count: 2/4 launched while
    # "searching" is still an R2 restore-to-four.
    s = _status(launched=2, status="Searching game...")
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_down_marker_beats_operational_substring():
    # A readable down-state like "Not live" contains the substring "live" but must
    # NOT be read as operational — it relaunches (R2).
    s = _status(launched=4, status="Not live")
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_offline_was_live_relaunches():
    # "Offline (was live 5m ago)" contains both "offline" and "live"; down wins.
    s = _status(launched=4, status="Offline (was live 5m ago)")
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_r3_idle_score_unchanged():
    s = _status(launched=4, status="LIVE", map="de_nuke", score="[1:0]", in_match=True)
    st = pr.observe(s, pr.PanelState(), 0.0, CFG)
    st = pr.observe(s, st, 5.0, CFG)
    d = pr.decide(s, 5.0, st, 11 * 60.0, CFG)
    assert d.actions == ["make_lobbies"]


def test_overlaunch_clock_resets_when_back_to_target():
    s_over = _status(launched=8, status="LIVE", in_match=True)
    s_ok = _status(launched=4, status="LIVE", map="m", score="[0:0]", in_match=True)
    st = pr.observe(s_over, pr.PanelState(), 0.0, CFG)
    st = pr.observe(s_ok, st, 30.0, CFG)
    assert st.over_launch_since is None


def test_unparseable_status_card_is_noop():
    # launched=None (a normal non-status message) -> noop, NOT a flag.
    s = _status(launched=None, status=None)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).kind == "noop"


def test_unparsed_status_at_target_is_healthy_noop():
    # launched==target, in a match, but the Status line didn't parse: must NOT be
    # read as 'not LIVE' and must NOT relaunch a healthy panel.
    s = _status(launched=4, status=None, map="de_nuke", score="[1:0]", in_match=True)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).kind == "noop"


def test_explicit_not_live_status_relaunches():
    s = _status(launched=4, status="OFFLINE", in_match=False)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).actions == ["select_unfarmed", "start_selected"]


def test_idle_requires_confirmed_live():
    # status unknown + not in a match -> cannot confirm LIVE -> noop, not make_lobbies.
    s = _status(launched=4, status=None, in_match=False)
    assert pr.decide(s, 5.0, pr.PanelState(), 1000.0, CFG).kind == "noop"


# --- launch grace (the SF21 lesson: launches take minutes, don't relaunch) ----

GRACE_CFG = SimpleNamespace(panel_target_accounts=4, panel_overlaunch_minutes=15,
                            panel_idle_minutes=10, panel_stale_minutes=30,
                            panel_action_debounce_seconds=180,
                            panel_launch_grace_minutes=15)


def test_launching_within_grace_is_noop():
    s = _status(launched=1, status="Accounts launching...")
    st = pr.observe(s, pr.PanelState(), 1000.0, GRACE_CFG)
    assert st.launching_since == 1000.0
    d = pr.decide(s, 5.0, st, 1000.0 + 8 * 60, GRACE_CFG)   # 8 min in: wait
    assert d.kind == "noop" and "launch" in (d.reason or "")


def test_launching_past_grace_relaunches():
    s = _status(launched=1, status="Accounts launching...")
    st = pr.observe(s, pr.PanelState(), 1000.0, GRACE_CFG)
    d = pr.decide(s, 5.0, st, 1000.0 + 20 * 60, GRACE_CFG)  # 20 min: stuck
    assert d.kind == "sequence" and d.actions == ["select_unfarmed", "start_selected"]


def test_operational_clears_launching_since():
    st = pr.PanelState(launching_since=1000.0)
    s = _status(launched=4, status="LIVE", in_match=True)
    st = pr.observe(s, st, 2000.0, GRACE_CFG)
    assert st.launching_since is None


def test_r1_overlaunch_still_wins_while_launching():
    st = pr.PanelState(launching_since=1000.0, over_launch_since=0.0)
    s = _status(launched=6, status="Accounts launching...")
    d = pr.decide(s, 5.0, st, 0.0 + 16 * 60, GRACE_CFG)      # over-launch >15m
    assert d.kind == "sequence" and "kill_all" in d.actions
