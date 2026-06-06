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
