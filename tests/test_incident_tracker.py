import os

import pytest

from watcherdog.incident_tracker import IncidentTracker


@pytest.fixture
def tracker(tmp_path):
    t = IncidentTracker(os.path.join(str(tmp_path), "data", "incidents.db"))
    yield t
    t.close()


def test_open_is_idempotent_per_key(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=False, now=100.0)
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=False, now=130.0)
    assert len(tracker.open_list()) == 1


def test_resolve_by_bot_returns_elapsed_and_fix_flag(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=True, fix_attempted="relaunch", now=100.0)
    res = tracker.resolve_by_bot("bot_error", "Bot1", "we_fixed", now=280.0)
    assert res["elapsed"] == pytest.approx(180.0)
    assert res["fix_attempted"] == "relaunch"
    assert tracker.open_list() == []


def test_resolve_by_bot_none_when_nothing_open(tracker):
    assert tracker.resolve_by_bot("bot_error", "Ghost", "self_healed", now=1.0) is None


def test_reopen_after_resolve_starts_fresh_episode(tracker):
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=100.0)
    tracker.resolve_by_bot("silence", "Bot1", "self_healed", now=200.0)
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet again",
                 fixable=False, now=300.0)
    rows = tracker.open_list()
    assert len(rows) == 1
    assert rows[0]["opened_ts"] == 300.0


def test_due_for_followup_honours_interval(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    assert tracker.due_for_followup(900, now=800.0) == []
    assert len(tracker.due_for_followup(900, now=900.0)) == 1


def test_mark_followed_up_resets_the_clock(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    tracker.mark_followed_up("k", now=900.0)
    assert tracker.due_for_followup(900, now=1000.0) == []
    assert len(tracker.due_for_followup(900, now=1800.0)) == 1


def test_due_for_giveup_uses_opened_ts(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    tracker.mark_followed_up("k", now=3000.0)   # nagging must not delay give-up
    assert len(tracker.due_for_giveup(3600, now=3600.0)) == 1


def test_note_fix_attempt_bumps_retries(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=True, now=0.0)
    tracker.note_fix_attempt("k", "relaunch")
    tracker.note_fix_attempt("k", "relaunch")
    assert tracker.open_list()[0]["fix_retries"] == 2


def test_escalate_removes_from_open(tracker):
    tracker.open("panel", "Bot1", "k", "high", "PC OFF", fixable=False, now=0.0)
    tracker.escalate("k", now=3600.0)
    assert tracker.open_list() == []


from watcherdog.incident_tracker import incident_followup_step


def _kinds(actions):
    return [(a["kind"], a["row"]["bot"]) for a in actions]


def test_planner_nags_non_fixable_at_interval(tracker):
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=0.0)
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("followup", "Bot1")]


def test_planner_refixes_fixable_bot_error_with_budget(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h", "high", "boom",
                 fixable=True, raw_excerpt="boom", now=0.0)
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("refix", "Bot1")]


def test_planner_falls_back_to_nag_when_retry_budget_spent(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "boom",
                 fixable=True, now=0.0)
    tracker.note_fix_attempt("k", "x")
    tracker.note_fix_attempt("k", "x")   # retries now == 2 == cap
    actions = incident_followup_step(tracker, now=900.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("followup", "Bot1")]


def test_planner_giveup_wins_over_followup(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "boom",
                 fixable=True, now=0.0)
    actions = incident_followup_step(tracker, now=3600.0,
                                     followup_interval_s=900,
                                     giveup_s=3600, max_fix_retries=2)
    assert _kinds(actions) == [("giveup", "Bot1")]
