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


def test_refresh_updates_open_row_in_place(tracker):
    # A worse/distinct error superseding the original must UPDATE the open row in
    # place (open() is idempotent and would NOT change it). Same row id, still open.
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "medium", "launch error",
                 fixable=False, now=100.0)
    before = tracker.open_for_bot("bot_error", "Bot1")
    out = tracker.refresh("bot_error:Bot1", "critical", "ACCOUNT BANNED",
                          raw_excerpt="banned")
    assert out is not None
    after = tracker.open_for_bot("bot_error", "Bot1")
    assert after["severity"] == "critical"
    assert after["summary"] == "ACCOUNT BANNED"
    assert after["raw_excerpt"] == "banned"
    assert after["status"] == "open"
    assert after["id"] == before["id"]          # same row, updated in place
    assert len(tracker.open_list()) == 1


def test_refresh_noop_on_unknown_or_closed_key(tracker):
    # No open row for the key → refresh is a no-op (returns None, no error).
    assert tracker.refresh("bot_error:Ghost", "critical", "x") is None
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=100.0)
    tracker.resolve_by_bot("bot_error", "Bot1", "self_healed", now=200.0)
    assert tracker.refresh("bot_error:Bot1", "critical", "x") is None   # closed → no-op


def test_resolve_by_bot_returns_elapsed_and_fix_flag(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1:h1", "high", "boom",
                 fixable=True, fix_attempted="relaunch", now=100.0)
    res = tracker.resolve_by_bot("bot_error", "Bot1", "we_fixed", now=280.0)
    assert res["elapsed"] == pytest.approx(180.0)
    assert res["fix_attempted"] == "relaunch"
    assert tracker.open_list() == []


def test_resolve_by_bot_none_when_nothing_open(tracker):
    assert tracker.resolve_by_bot("bot_error", "Ghost", "self_healed", now=1.0) is None


def test_resolve_by_bot_is_scoped_to_source(tracker):
    # Pins the scoping property the silence-recovery fix relies on: a bot can have
    # BOTH a silence incident and a bot_error incident open at once. Closing the
    # SILENCE source (e.g. the bot went quiet then a message arrives) must resolve
    # ONLY the silence row and leave the bot_error untouched — that bot_error may
    # have arrived as the very traffic that ended the silence and is a distinct
    # failure mode/channel that is still unresolved.
    tracker.open("silence", "Bot1", "silence:Bot1", "high", "quiet",
                 fixable=False, now=100.0)
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=120.0)

    res = tracker.resolve_by_bot("silence", "Bot1", "self_healed", now=200.0)

    assert res is not None                                       # silence WAS open
    assert tracker.open_for_bot("silence", "Bot1") is None       # silence resolved
    bot_err = tracker.open_for_bot("bot_error", "Bot1")
    assert bot_err is not None                                   # bot_error STILL open
    assert bot_err["status"] == "open"


def test_resolve_open_for_bot_closes_all_sources(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "x", fixable=False, now=100.0)
    tracker.open("panel", "Bot1", "panel:Bot1", "high", "pc", fixable=False, now=120.0)
    res = tracker.resolve_open_for_bot("Bot1", "self_healed", now=400.0)
    assert res is not None
    assert res["count"] == 2
    assert res["elapsed"] == pytest.approx(300.0)   # from EARLIEST opened_ts (100)
    assert tracker.open_list() == []


def test_resolve_open_for_bot_none_when_nothing_open(tracker):
    assert tracker.resolve_open_for_bot("Ghost", "self_healed", now=1.0) is None


def test_resolve_open_for_bot_we_fixed_when_any_attempt_fixed(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "x", fixable=True, now=100.0)
    tracker.note_fix_attempt("bot_error:Bot1", "fixed")
    res = tracker.resolve_open_for_bot("Bot1", "we_fixed", now=200.0)
    assert res["we_fixed"] is True


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


# ---------------------------------------------------------------------------
# by-id mutations — the race fix. The follow-up tick snapshots a row, AWAITS
# network/buttons (tens of seconds), then mutates. If a monitor sweep
# resolves+REOPENS the same key during the await, a KEY-based mutation lands on
# the NEW row. These id-targeted methods + the `AND status='open'` guard make a
# mutation on an already-resolved id a safe NO-OP, so the old episode's id never
# touches the fresh row's budget.
# ---------------------------------------------------------------------------

def test_get_open_by_id_returns_row_for_open_id(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=100.0)
    opened = tracker.open_for_bot("bot_error", "Bot1")
    row = tracker.get_open_by_id(opened["id"])
    assert row is not None
    assert row["id"] == opened["id"]
    assert row["key"] == "bot_error:Bot1"
    assert row["status"] == "open"


def test_get_open_by_id_none_for_unknown_id(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=100.0)
    opened = tracker.open_for_bot("bot_error", "Bot1")
    assert tracker.get_open_by_id(opened["id"] + 999) is None


def test_get_open_by_id_none_after_resolved(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=False, now=100.0)
    opened = tracker.open_for_bot("bot_error", "Bot1")
    tracker.resolve_by_bot("bot_error", "Bot1", "self_healed", now=200.0)
    assert tracker.get_open_by_id(opened["id"]) is None   # resolved → not open


def test_note_fix_attempt_by_id_bumps_retries(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=True, now=0.0)
    iid = tracker.open_for_bot("bot_error", "Bot1")["id"]
    tracker.note_fix_attempt_by_id(iid, "relaunch")
    tracker.note_fix_attempt_by_id(iid, "relaunch")
    row = tracker.get_open_by_id(iid)
    assert row["fix_retries"] == 2
    assert row["fix_attempted"] == "relaunch"


def test_mark_followed_up_by_id_resets_the_clock(tracker):
    tracker.open("bot_error", "Bot1", "k", "high", "x", fixable=False, now=0.0)
    iid = tracker.open_for_bot("bot_error", "Bot1")["id"]
    tracker.mark_followed_up_by_id(iid, now=900.0)
    row = tracker.get_open_by_id(iid)
    assert row["update_count"] == 1
    assert row["last_update_ts"] == 900.0
    assert tracker.due_for_followup(900, now=1000.0) == []
    assert len(tracker.due_for_followup(900, now=1800.0)) == 1


def test_escalate_by_id_removes_from_open(tracker):
    tracker.open("panel", "Bot1", "k", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "Bot1")["id"]
    tracker.escalate_by_id(iid, now=3600.0)
    assert tracker.open_list() == []
    assert tracker.get_open_by_id(iid) is None


def test_by_id_mutations_noop_on_resolved_id(tracker):
    # THE RACE FIX. Open an incident, snapshot its id, then resolve it (as a
    # monitor sweep would mid-await). Every _by_id mutation on the now-resolved
    # id must be a SILENT NO-OP — the resolved row's fields stay frozen and no
    # exception is raised. This is what prevents the old episode's id from
    # pre-burning a freshly-reopened row's budget.
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=True, fix_attempted="relaunch", now=100.0)
    iid = tracker.open_for_bot("bot_error", "Bot1")["id"]
    res = tracker.resolve_by_bot("bot_error", "Bot1", "self_healed", now=200.0)
    assert res is not None

    # All four no-op against a resolved id (no exceptions).
    assert tracker.get_open_by_id(iid) is None
    tracker.note_fix_attempt_by_id(iid, "should-not-apply")
    tracker.mark_followed_up_by_id(iid, now=500.0)
    tracker.escalate_by_id(iid, now=600.0)

    # The resolved row is untouched: still resolved (not 'escalated'), fields
    # frozen at their resolve-time values.
    after = tracker.conn.execute(
        "SELECT * FROM open_incidents WHERE id = ?", (iid,)).fetchone()
    assert after["status"] == "resolved"             # escalate_by_id no-op'd
    assert after["resolution"] == "self_healed"      # NOT 'gave_up'
    assert after["fix_attempted"] == "relaunch"      # NOT "should-not-apply"
    assert after["fix_retries"] == 0                 # note_fix_attempt no-op'd
    assert after["update_count"] == 0                # mark_followed_up no-op'd
    assert after["resolved_ts"] == 200.0             # escalate did NOT bump it


def test_by_id_mutation_on_resolved_id_spares_reopened_row(tracker):
    # The end-to-end race property: a stale id from a resolved episode must NOT
    # mutate the FRESH row reopened under the same key. Old id no-ops; new row
    # keeps its pristine budget.
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
                 fixable=True, now=100.0)
    old_id = tracker.open_for_bot("bot_error", "Bot1")["id"]
    tracker.resolve_by_bot("bot_error", "Bot1", "self_healed", now=200.0)
    # A new sweep reopens the same key — a brand-new episode/row.
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom again",
                 fixable=True, now=300.0)
    new_row = tracker.open_for_bot("bot_error", "Bot1")
    assert new_row["id"] != old_id

    # Mutating the STALE id must leave the fresh row entirely alone.
    tracker.note_fix_attempt_by_id(old_id, "x")
    tracker.mark_followed_up_by_id(old_id, now=400.0)
    tracker.escalate_by_id(old_id, now=500.0)

    fresh = tracker.open_for_bot("bot_error", "Bot1")
    assert fresh["id"] == new_row["id"]
    assert fresh["status"] == "open"
    assert fresh["fix_retries"] == 0
    assert fresh["update_count"] == 0


# ---------------------------------------------------------------------------
# dry-run isolation — a rehearsal (--dry-run) shares the SAME prod DB file but
# must NEVER write the live ledger. dry_run=True makes every MUTATING method a
# no-op while QUERIES stay live (they read the unchanged DB). This stops a
# dry-run sweep/followup from opening rows or escalating real open incidents to
# 'gave_up' and corrupting live state.
# ---------------------------------------------------------------------------

def test_dry_run_tracker_writes_nothing(tmp_path):
    t = IncidentTracker(str(tmp_path / "dry.db"), dry_run=True)
    t.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
           fixable=False, now=1.0)
    t.resolve_by_bot("bot_error", "Bot1", "self_healed", now=2.0)
    t.resolve_open_for_bot("Bot1", "self_healed", now=2.5)
    t.refresh("bot_error:Bot1", "critical", "x")
    t.note_fix_attempt("bot_error:Bot1", "retry")
    t.mark_followed_up("bot_error:Bot1", now=3.0)
    t.escalate("bot_error:Bot1", now=4.0)
    t.note_fix_attempt_by_id(1, "retry")
    t.mark_followed_up_by_id(1, now=5.0)
    t.escalate_by_id(1, now=6.0)
    assert t.open_list() == []           # nothing was ever written
    t.close()


def test_dry_run_does_not_mutate_existing_rows(tmp_path):
    # The real danger: a dry-run run against the prod DB must not touch rows a
    # PRIOR live run opened. Seed a real open row, then drive every UPDATE-style
    # mutator from a dry_run tracker and assert the row is byte-for-byte unchanged.
    db = str(tmp_path / "shared.db")
    live = IncidentTracker(db)
    live.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom", fixable=False, now=1.0)
    rid = live.open_for_bot("bot_error", "Bot1")["id"]
    live.close()

    dry = IncidentTracker(db, dry_run=True)
    dry.refresh("bot_error:Bot1", "critical", "BANNED")
    dry.note_fix_attempt("bot_error:Bot1", "retry")
    dry.mark_followed_up("bot_error:Bot1", now=9.0)
    dry.note_fix_attempt_by_id(rid, "retry")
    dry.mark_followed_up_by_id(rid, now=9.0)
    dry.escalate("bot_error:Bot1", now=9.0)
    dry.escalate_by_id(rid, now=9.0)
    dry.resolve_by_bot("bot_error", "Bot1", "self_healed", now=9.0)
    dry.resolve_open_for_bot("Bot1", "self_healed", now=9.0)
    dry.close()

    check = IncidentTracker(db)
    after = check.open_for_bot("bot_error", "Bot1")
    assert after is not None                 # not escalated/resolved away
    assert after["status"] == "open"
    assert after["severity"] == "high"       # refresh no-op'd
    assert after["summary"] == "boom"
    assert after["fix_retries"] == 0         # note_fix_attempt* no-op'd
    assert after["update_count"] == 0        # mark_followed_up* no-op'd
    check.close()


def test_live_tracker_still_writes(tmp_path):
    t = IncidentTracker(str(tmp_path / "live.db"))   # dry_run defaults False
    t.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom",
           fixable=False, now=1.0)
    assert len(t.open_list()) == 1
    t.close()


def test_dry_run_queries_stay_live(tmp_path):
    # Queries must keep working against the (empty) DB without error — only the
    # MUTATING methods are gated. A dry-run tracker reading an empty ledger
    # returns the natural empty/None results, never an exception.
    t = IncidentTracker(str(tmp_path / "dry_q.db"), dry_run=True)
    assert t.open_for_bot("bot_error", "Bot1") is None
    assert t.open_list() == []
    assert t.open_list_for_bot("Bot1") == []
    assert t.get_open_by_id(1) is None
    assert t.due_for_followup(900, now=900.0) == []
    assert t.due_for_giveup(3600, now=3600.0) == []
    t.close()


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


# --- Phase 4: novel flag + migration -----------------------------------------

def test_migration_adds_novel_column_to_old_db(tmp_path):
    """A pre-Phase-4 DB (no `novel` column) upgrades in place; old rows read novel=0."""
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE open_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
            source TEXT NOT NULL, bot TEXT NOT NULL, severity TEXT, summary TEXT,
            raw_excerpt TEXT, fixable INTEGER NOT NULL DEFAULT 0,
            fix_attempted TEXT, fix_retries INTEGER NOT NULL DEFAULT 0,
            opened_ts REAL NOT NULL, last_update_ts REAL NOT NULL,
            update_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open', resolved_ts REAL, resolution TEXT)
    """)
    conn.execute(
        "INSERT INTO open_incidents (key, source, bot, severity, summary, fixable,"
        " opened_ts, last_update_ts, update_count, status)"
        " VALUES ('bot_error:SF1','bot_error','SF1','high','old row',0,1.0,1.0,0,'open')")
    conn.commit(); conn.close()
    t = IncidentTracker(db)
    row = t.open_for_bot("bot_error", "SF1")
    assert row["novel"] == 0                 # old row readable, defaulted
    assert t.novel_list() == []
    t.close()


def test_open_novel_flag_and_novel_list(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird new error",
           fixable=True, novel=True, now=100.0)
    t.open("bot_error", "SF8", "bot_error:SF8", "high", "known error",
           fixable=True, now=101.0)          # default novel=False
    novel = t.novel_list()
    assert [r["bot"] for r in novel] == ["SF7"]
    assert novel[0]["novel"] == 1
    assert t.open_for_bot("bot_error", "SF8")["novel"] == 0
    t.close()


def test_resolve_by_id_only_open_rows(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    row = t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird",
                 fixable=True, novel=True, now=100.0)
    assert t.resolve_by_id(row["id"], "overseer_fixed", now=200.0) is True
    assert t.open_for_bot("bot_error", "SF7") is None          # closed
    assert t.resolve_by_id(row["id"], "again", now=300.0) is False  # already resolved
    t.close()


def test_escalated_list_returns_escalated_with_since_filter(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker
    tr = IncidentTracker(str(tmp_path / "i.db"))
    tr.open("panel", "SinFermera11", "panel:SinFermera11", "high", "needs PC",
            fixable=False, novel=True, now=100.0)
    # not escalated yet → empty
    assert tr.escalated_list() == []
    tr.escalate("panel:SinFermera11", now=200.0)
    assert [r["bot"] for r in tr.escalated_list()] == ["SinFermera11"]
    assert [r["bot"] for r in tr.escalated_list(since=150.0)] == ["SinFermera11"]
    assert tr.escalated_list(since=300.0) == []   # escalated (ts=200) is before the window
    tr.close()


# ---------------------------------------------------------------------------
# 'parked' terminal status — a PANEL cold-case whose only fix is a human turning
# the PC on. park_by_id() retires the row (status='parked'), parked_by_key()
# powers the churn-killing dedup, parked_list() persists the latch across a
# restart, and touch() bumps a parked row's heartbeat without re-INSERTing.
# ---------------------------------------------------------------------------

def test_park_by_id_sets_parked_status_and_needs_pc(tracker):
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=3600.0)
    # Off the open queue (like escalate) but NOT escalated — its own terminal status.
    assert tracker.open_list() == []
    assert tracker.get_open_by_id(iid) is None
    row = tracker.conn.execute(
        "SELECT * FROM open_incidents WHERE id = ?", (iid,)).fetchone()
    assert row["status"] == "parked"
    assert row["resolution"] == "needs_pc"
    assert row["resolved_ts"] == 3600.0


def test_park_by_id_noop_on_resolved_id(tracker):
    # Mirror escalate_by_id: a park on an already-closed id is a silent NO-OP so a
    # stale snapshot never re-parks a freshly-reopened row.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.resolve_by_bot("panel", "SF11", "self_healed", now=100.0)
    tracker.park_by_id(iid, now=200.0)
    row = tracker.conn.execute(
        "SELECT * FROM open_incidents WHERE id = ?", (iid,)).fetchone()
    assert row["status"] == "resolved"          # park no-op'd against the resolved id
    assert row["resolution"] == "self_healed"


def test_parked_by_key_returns_newest_parked_or_none(tracker):
    assert tracker.parked_by_key("panel:SF11") is None     # nothing parked
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    assert tracker.parked_by_key("panel:SF11") is None     # open, not yet parked
    tracker.park_by_id(iid, now=3600.0)
    parked = tracker.parked_by_key("panel:SF11")
    assert parked is not None
    assert parked["id"] == iid
    assert parked["summary"] == "PC OFF"
    # A different key is unaffected.
    assert tracker.parked_by_key("panel:SF14") is None


def test_parked_by_key_scoped_strictly_to_key(tracker):
    # The guard the reviewer attacks: parked dedup must key STRICTLY on
    # panel:{name}; a bot_error key for the SAME bot must NOT see the parked row.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=10.0)
    assert tracker.parked_by_key("panel:SF11") is not None
    assert tracker.parked_by_key("bot_error:SF11") is None   # different key/source


def test_touch_bumps_last_update_without_new_row(tracker):
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=10.0)
    before = tracker.parked_by_key("panel:SF11")
    tracker.touch(iid, now=999.0)
    after = tracker.parked_by_key("panel:SF11")
    assert after["id"] == before["id"]            # same row, no INSERT
    assert after["last_update_ts"] == 999.0       # heartbeat bumped
    assert after["status"] == "parked"            # still parked (touch ≠ resolve/escalate)
    assert after["resolved_ts"] == 10.0           # park time untouched (the "since" clock)
    # Touch must not add a row.
    all_rows = tracker.conn.execute(
        "SELECT COUNT(*) c FROM open_incidents WHERE key = 'panel:SF11'").fetchone()
    assert all_rows["c"] == 1


def test_parked_list_returns_parked_newest_first(tracker):
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    tracker.open("panel", "SF14", "panel:SF14", "high", "black screen", fixable=False, now=1.0)
    iid11 = tracker.open_for_bot("panel", "SF11")["id"]
    iid14 = tracker.open_for_bot("panel", "SF14")["id"]
    assert tracker.parked_list() == []
    tracker.park_by_id(iid11, now=100.0)
    tracker.park_by_id(iid14, now=200.0)   # newer park time
    parked = tracker.parked_list()
    assert [r["bot"] for r in parked] == ["SF14", "SF11"]   # newest park first


def test_parked_rows_not_in_open_novel_or_escalated_lists(tracker):
    # GUARD: parked is a DISTINCT status — it must not leak into the open queue,
    # the flagged (novel) queue, or the escalated_recent queue.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF",
                 fixable=False, novel=True, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=100.0)
    assert tracker.open_list() == []
    assert tracker.novel_list() == []            # flagged must NOT include parked
    assert tracker.escalated_list() == []        # escalated_recent must NOT include parked
    assert [r["bot"] for r in tracker.parked_list()] == ["SF11"]


def test_resolve_open_for_bot_default_does_not_clear_parked(tracker):
    # THE BLOCKER GUARD: the DEFAULT (include_parked=False) must clear ONLY
    # status='open' rows and NEVER a parked row. The _evaluate_bot chatter path
    # uses the default, so a benign "normal" line on a panel name must not
    # false-clear a parked PC-off panel.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=100.0)
    assert tracker.parked_by_key("panel:SF11") is not None
    # Default call: nothing OPEN for the bot → returns None, parked row untouched.
    assert tracker.resolve_open_for_bot("SF11", "self_healed", now=500.0) is None
    assert tracker.parked_by_key("panel:SF11") is not None   # STILL parked
    row = tracker.conn.execute(
        "SELECT * FROM open_incidents WHERE id = ?", (iid,)).fetchone()
    assert row["status"] == "parked"


def test_resolve_open_for_bot_default_clears_open_leaves_parked(tracker):
    # Default also must not sweep a parked row while clearing a real open one.
    tracker.open("bot_error", "SF11", "bot_error:SF11", "high", "boom", fixable=False, now=0.0)
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=10.0)
    pid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(pid, now=100.0)
    res = tracker.resolve_open_for_bot("SF11", "self_healed", now=500.0)  # default
    assert res is not None and res["count"] == 1            # ONLY the open bot_error
    assert tracker.open_for_bot("bot_error", "SF11") is None
    assert tracker.parked_by_key("panel:SF11") is not None  # parked SURVIVES the default


def test_resolve_open_for_bot_include_parked_clears_parked(tracker):
    # Real recovery via the freshness-gated panel-healthy path: include_parked=True
    # closes a PARKED panel row too (the PC came back) so it leaves needs_human.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=100.0)
    assert tracker.parked_by_key("panel:SF11") is not None
    res = tracker.resolve_open_for_bot("SF11", "self_healed", now=500.0, include_parked=True)
    assert res is not None and res["count"] == 1
    assert tracker.parked_by_key("panel:SF11") is None      # parked row closed
    row = tracker.conn.execute(
        "SELECT * FROM open_incidents WHERE id = ?", (iid,)).fetchone()
    assert row["status"] == "resolved"


def test_resolve_open_for_bot_include_parked_clears_open_and_parked_together(tracker):
    # A bot with BOTH an open bot_error and a parked panel row: include_parked
    # closes both.
    tracker.open("bot_error", "SF11", "bot_error:SF11", "high", "boom", fixable=False, now=0.0)
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=10.0)
    pid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(pid, now=100.0)
    res = tracker.resolve_open_for_bot("SF11", "self_healed", now=500.0, include_parked=True)
    assert res is not None and res["count"] == 2            # open + parked both closed
    assert tracker.open_for_bot("bot_error", "SF11") is None
    assert tracker.parked_by_key("panel:SF11") is None


def test_resolve_open_for_bot_include_parked_does_not_clear_escalated(tracker):
    # GUARD: include_parked must NOT also start clearing 'escalated' rows —
    # only open + parked. A bot_error escalation stays escalated.
    tracker.open("bot_error", "SF11", "bot_error:SF11", "high", "boom", fixable=False, now=0.0)
    tracker.escalate("bot_error:SF11", now=100.0)
    assert tracker.resolve_open_for_bot(
        "SF11", "self_healed", now=500.0, include_parked=True) is None  # nothing open/parked
    assert [r["bot"] for r in tracker.escalated_list()] == ["SF11"]    # still escalated


def test_update_summary_edits_parked_row(tracker):
    # The "different cold-case" path records the new failure onto the parked row.
    tracker.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=0.0)
    iid = tracker.open_for_bot("panel", "SF11")["id"]
    tracker.park_by_id(iid, now=10.0)
    tracker.update_summary(iid, "black screen (RDP frozen)")
    parked = tracker.parked_by_key("panel:SF11")
    assert parked["summary"] == "black screen (RDP frozen)"
    assert parked["status"] == "parked"          # status untouched


def test_park_and_touch_are_dry_run_safe(tmp_path):
    # A rehearsal sharing the prod DB must never write the ledger via the new
    # mutators (mirror the existing dry-run isolation tests).
    db = str(tmp_path / "shared.db")
    live = IncidentTracker(db)
    live.open("panel", "SF11", "panel:SF11", "high", "PC OFF", fixable=False, now=1.0)
    rid = live.open_for_bot("panel", "SF11")["id"]
    live.close()

    dry = IncidentTracker(db, dry_run=True)
    dry.park_by_id(rid, now=9.0)
    dry.touch(rid, now=9.0)
    dry.update_summary(rid, "MUTATED")
    dry.close()

    check = IncidentTracker(db)
    after = check.open_for_bot("panel", "SF11")
    assert after is not None and after["status"] == "open"   # park no-op'd
    assert after["last_update_ts"] == 1.0                    # touch no-op'd
    assert after["summary"] == "PC OFF"                      # update_summary no-op'd
    check.close()
