"""Tests for the standalone overseer health probe (no socket, no Telethon)."""
import os
import sys
import types

import scripts.overseer_health as oh
from watcherdog.incident_tracker import IncidentTracker


def test_beacon_age_s_absent_is_none():
    assert oh._beacon_age_s("/no/such/beacon", 1000.0) is None


def test_beacon_age_s_reads_mtime(tmp_path):
    p = tmp_path / "watcher_healthy"
    p.write_text("123 100\n")
    os.utime(p, (500.0, 500.0))
    assert oh._beacon_age_s(str(p), 700.0) == 200.0


def test_flagged_reads_novel_rows_from_db(tmp_path):
    db = str(tmp_path / "i.db")
    tr = IncidentTracker(db)
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high",
            "screen grab failed", fixable=False, novel=True)
    tr.close()
    out = oh._flagged(db)
    assert out["count"] == 1 and out["bots"] == ["SinFermera15"]


def test_flagged_bad_db_degrades(tmp_path):
    out = oh._flagged(str(tmp_path / "missing-dir" / "x.db"))
    assert out["count"] == 0 and "bots" in out


def test_last_sweep_parses_newest(tmp_path):
    log = tmp_path / "gui_run.log"
    log.write_text(
        "2026-06-13 23:51:07 INFO [x] Sweep: 24 chats, 19 healthy\n"
        "2026-06-13 23:53:10 INFO [x] Sweep: 24 chats, 24 healthy\n")
    assert oh._last_sweep(str(log)) == "23:53 (24 chats, 24 healthy)"


def test_last_sweep_absent_is_none(tmp_path):
    assert oh._last_sweep(str(tmp_path / "none.log")) is None


def test_recent_errors_newest_first_bounded(tmp_path):
    log = tmp_path / "telegram.err.log"
    lines = [f"line {i}\n" for i in range(3)]
    lines += ["Traceback (most recent call last):\n",
              "ERROR boom one\n", "ERROR boom two\n"]
    log.write_text("".join(lines))
    errs = oh._recent_errors([str(log)], limit=2)
    assert errs == ["ERROR boom two", "ERROR boom one"]


def _cfg(tmp_path, **kw):
    base = dict(db_path=str(tmp_path / "i.db"),
                watcher_health_path=str(tmp_path / "watcher_healthy"),
                watch_poll_interval=120.0,
                gui_run_log=str(tmp_path / "gui_run.log"),
                overseer_socket="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_build_report_all_healthy(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1100.0, alive_fn=lambda: True)
    assert code == 0 and report["healthy"] is True
    assert report["process_alive"] is True and report["wedged"] is False
    assert report["flagged"]["count"] == 0


def test_build_report_process_dead_exit_1(tmp_path):
    report, code = oh.build_report(_cfg(tmp_path), 1100.0, alive_fn=lambda: False)
    assert code == 1 and report["healthy"] is False
    assert report["process_alive"] is False


def test_build_report_wedged_beacon(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1700.0, alive_fn=lambda: True)
    assert report["wedged"] is True and code == 1


def test_build_report_fresh_beacon_not_wedged(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["wedged"] is False and code == 0


def test_build_report_flagged_triggers_exit_1(tmp_path):
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high", "x",
            fixable=False, novel=True)
    tr.close()
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert code == 1 and report["flagged"]["bots"] == ["SinFermera15"]


def test_build_report_socket_present_is_report_only(tmp_path):
    sock = tmp_path / "overseer.sock"
    sock.write_text("")
    cfg = _cfg(tmp_path, overseer_socket=str(sock))
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["socket_present"] is True
    assert code == 0


def test_build_report_recent_errors_report_only(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "telegram.err.log").write_text("ERROR something bad\n")
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["recent_errors"] == ["ERROR something bad"]
    assert code == 0


def test_build_report_json_serializable_and_mirrors_exit(tmp_path):
    report, code = oh.build_report(_cfg(tmp_path), 1100.0, alive_fn=lambda: False)
    import json as _json
    _json.dumps(report)
    assert report["healthy"] == (code == 0)


def test_conftest_puts_repo_root_first():
    # Regression: an external PYTHONPATH entry must not shadow repo packages.
    root = os.path.dirname(os.path.dirname(os.path.abspath(oh.__file__)))
    assert sys.path[0] == root


def test_proc_pattern_matches_python_not_carriers():
    # The pattern must match a real "python … run_watcher.py" command line but NOT
    # carriers that merely mention the script (editor/tail/shell history) — those
    # would falsely report a dead watcher as alive.
    import re
    pat = oh._PROC_PATTERN
    for c in ["/opt/homebrew/.../Python run_watcher.py --verbose",
              "python3 /Users/x/proj/run_watcher.py",
              ".venv/bin/python run_watcher.py"]:
        assert re.search(pat, c), f"should match: {c!r}"
    for c in ["vim run_watcher.py",
              "/usr/bin/tail -f /Users/x/proj/data/run_watcher.py",
              "less run_watcher.py"]:
        assert not re.search(pat, c), f"should NOT match: {c!r}"


def test_build_report_dead_with_stale_beacon_exit_1_not_wedged(tmp_path):
    # A dead watcher with a stale leftover beacon → unhealthy (exit 1) via BOTH
    # not-alive and beacon_stale; `wedged` stays False (its meaning is "alive but
    # not heartbeating"), proving the wake trigger is decoupled from `alive`.
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (1000.0, 1000.0))                 # 700s old > 5*120
    report, code = oh.build_report(cfg, 1700.0, alive_fn=lambda: False)
    assert code == 1
    assert report["process_alive"] is False
    assert report["wedged"] is False                   # dead, not "wedged"
    assert report["healthy"] is False


def test_build_report_escalated_recent_is_report_only(tmp_path):
    # An escalated-but-still-down panel drops out of `flagged` (status!='open'), so
    # `healthy` can be True; `escalated_recent` restores VISIBILITY without flipping
    # the exit code (the human was already alerted).
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera11", "panel:SinFermera11", "high", "needs PC",
            fixable=False, novel=True, now=1000.0)
    tr.escalate("panel:SinFermera11", now=1500.0)
    tr.close()
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n"); os.utime(beacon, (1500.0, 1500.0))
    report, code = oh.build_report(cfg, 1560.0, alive_fn=lambda: True)
    assert report["flagged"]["count"] == 0                 # escalated out of the open queue
    assert report["escalated_recent"]["bots"] == ["SinFermera11"]
    assert report["healthy"] is True and code == 0         # context only, not a wake trigger


# ---------------------------------------------------------------------------
# needs_human — parked (PC-off) panels. Report-only, NEVER fades, and MUST NOT
# flip `healthy`/the exit code (a parked panel is human-owned; surfacing it must
# never re-wake Hermes in a loop). Mirrors the escalated_recent rationale.
# ---------------------------------------------------------------------------

def test_parked_reads_parked_rows_with_park_hours(tmp_path):
    db = str(tmp_path / "i.db")
    tr = IncidentTracker(db)
    tr.open("panel", "SinFermera14", "panel:SinFermera14", "high",
            "PC OFF", fixable=False, novel=True, now=1000.0)
    iid = tr.open_for_bot("panel", "SinFermera14")["id"]
    tr.park_by_id(iid, now=2000.0)        # parked at t=2000
    tr.close()
    # 2h after parking (now - resolved_ts = 7200s = 2.0h).
    out = oh._parked(db, 2000.0 + 7200.0)
    assert out["count"] == 1
    assert out["bots"] == ["SinFermera14"]
    assert out["stale"][0]["bot"] == "SinFermera14"
    assert out["stale"][0]["parked_h"] == 2.0


def test_parked_bad_db_degrades(tmp_path):
    out = oh._parked(str(tmp_path / "missing-dir" / "x.db"), 0.0)
    assert out["count"] == 0 and out["bots"] == [] and out["stale"] == []


def test_build_report_needs_human_is_report_only_no_fade(tmp_path):
    # A parked panel must appear in needs_human EVEN >24h after parking (no fade),
    # while `healthy` stays True and the exit code is 0 — it must never re-wake
    # Hermes. It also drops out of flagged (status!='open') and escalated_recent
    # (status!='escalated').
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera14", "panel:SinFermera14", "high", "PC OFF",
            fixable=False, novel=True, now=1000.0)
    iid = tr.open_for_bot("panel", "SinFermera14")["id"]
    tr.park_by_id(iid, now=2000.0)
    tr.close()
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    # 48h after parking — well past the 24h escalated window.
    now = 2000.0 + 48 * 3600.0
    os.utime(beacon, (now, now))
    report, code = oh.build_report(cfg, now, alive_fn=lambda: True)
    assert report["flagged"]["count"] == 0                  # parked → out of open queue
    assert report["escalated_recent"]["count"] == 0         # parked != escalated
    assert report["needs_human"]["bots"] == ["SinFermera14"]
    assert report["needs_human"]["stale"][0]["parked_h"] == 48.0   # no fade
    assert report["healthy"] is True and code == 0          # report-only, no wake
    import json as _json
    _json.dumps(report)                                     # still serialisable


def test_build_report_needs_human_empty_when_no_parked(tmp_path):
    cfg = _cfg(tmp_path)
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n"); os.utime(beacon, (1000.0, 1000.0))
    report, code = oh.build_report(cfg, 1060.0, alive_fn=lambda: True)
    assert report["needs_human"] == {"count": 0, "bots": [], "stale": []}
    assert code == 0


# ---------------------------------------------------------------------------
# flagged_stuck — Component A. The EXIT CODE now gates on *stuck* incidents
# (open longer than OVERSEER_STUCK_MIN) instead of any open incident, so the
# probe never wakes the agent during the core's routine ~5-10 min auto-recovery
# ladder. The full `flagged` set stays in the JSON unchanged (visibility); a new
# `flagged_stuck` = {count, bots, stale:[{bot, open_min|null, reason?}]} carries
# the gated subset. Missing/unparseable opened_ts fails toward waking.
# ---------------------------------------------------------------------------

def _open_flagged(db_path, bot, *, now):
    """Open a novel/flagged incident at time `now` and close the tracker."""
    tr = IncidentTracker(db_path)
    tr.open("panel", bot, "panel:" + bot, "high", "screen grab failed",
            fixable=False, novel=True, now=now)
    tr.close()


def _beacon(tmp_path, mtime):
    beacon = tmp_path / "watcher_healthy"
    beacon.write_text("1 1\n")
    os.utime(beacon, (mtime, mtime))
    return beacon


def test_flagged_stuck_younger_than_threshold_inflight_exit_0(tmp_path):
    # A flagged incident YOUNGER than STUCK_MIN: the core is laddering it. It is
    # still surfaced in `flagged` (visibility), but `flagged_stuck` is empty and
    # the probe exits 0 / healthy — no wake during routine auto-recovery.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1300.0)
    # 5 min old (< default 12 min STUCK_MIN).
    report, code = oh.build_report(cfg, 1000.0 + 5 * 60, alive_fn=lambda: True)
    assert report["flagged"]["count"] == 1
    assert report["flagged"]["bots"] == ["SinFermera7"]
    assert report["flagged_stuck"]["count"] == 0
    assert report["flagged_stuck"]["bots"] == []
    assert report["flagged_stuck"]["stale"] == []
    assert code == 0 and report["healthy"] is True


def test_flagged_stuck_older_than_threshold_wakes_exit_1(tmp_path):
    # A flagged incident OLDER than STUCK_MIN → stuck → exit 1 / unhealthy, and
    # listed in flagged_stuck with open_min ~ its age in minutes.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1000.0 + 20 * 60)
    # 20 min old (> default 12 min STUCK_MIN).
    report, code = oh.build_report(cfg, 1000.0 + 20 * 60, alive_fn=lambda: True)
    assert code == 1 and report["healthy"] is False
    assert report["flagged"]["count"] == 1            # full set still reported
    assert report["flagged_stuck"]["count"] == 1
    assert report["flagged_stuck"]["bots"] == ["SinFermera7"]
    stale = report["flagged_stuck"]["stale"]
    assert stale[0]["bot"] == "SinFermera7"
    assert stale[0]["open_min"] == 20                 # int minutes open
    assert "reason" not in stale[0]                   # age is known


def test_flagged_stuck_age_unknown_fails_toward_waking(tmp_path):
    # An incident with an unparseable opened_ts cannot be proven in-flight → it
    # is counted as stuck with open_min==None, reason=="age_unknown", exit 1.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera9", now=1000.0)
    # Corrupt opened_ts to a non-numeric value (NOT NULL still satisfied).
    tr = IncidentTracker(cfg.db_path)
    tr.conn.execute(
        "UPDATE open_incidents SET opened_ts = 'garbage' WHERE bot = ?",
        ("SinFermera9",))
    tr.conn.commit()
    tr.close()
    _beacon(tmp_path, 1000.0)
    report, code = oh.build_report(cfg, 1000.0 + 60 * 60, alive_fn=lambda: True)
    assert code == 1 and report["healthy"] is False
    assert report["flagged_stuck"]["count"] == 1
    assert report["flagged_stuck"]["bots"] == ["SinFermera9"]
    entry = report["flagged_stuck"]["stale"][0]
    assert entry["bot"] == "SinFermera9"
    assert entry["open_min"] is None
    assert entry["reason"] == "age_unknown"


def test_flagged_stuck_null_opened_ts_fails_toward_waking(tmp_path):
    # A NULL opened_ts (the dict row carries None) is also unknown-age → stuck.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera10", now=1000.0)
    # SQLite enforces NOT NULL on opened_ts; emulate a missing age by storing an
    # empty string (parses to None) — the helper must still fail toward waking.
    tr = IncidentTracker(cfg.db_path)
    tr.conn.execute(
        "UPDATE open_incidents SET opened_ts = '' WHERE bot = ?",
        ("SinFermera10",))
    tr.conn.commit()
    tr.close()
    _beacon(tmp_path, 1000.0)
    report, code = oh.build_report(cfg, 1000.0 + 60 * 60, alive_fn=lambda: True)
    assert code == 1
    entry = report["flagged_stuck"]["stale"][0]
    assert entry["open_min"] is None and entry["reason"] == "age_unknown"


def test_flagged_stuck_dead_watcher_exit_1_regardless_of_age(tmp_path):
    # Dead watcher trips immediately even with a young (in-flight) incident.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1000.0 + 60)
    report, code = oh.build_report(cfg, 1000.0 + 60, alive_fn=lambda: False)
    assert code == 1 and report["healthy"] is False
    assert report["process_alive"] is False
    assert report["flagged_stuck"]["count"] == 0       # incident itself not stuck


def test_flagged_stuck_wedged_watcher_exit_1_regardless_of_age(tmp_path):
    # Wedged (alive but stale beacon) trips immediately even with a young incident.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1000.0)                            # beacon 60s old at start
    # now = 1000 + 700: beacon is 700s old > 5*120 → wedged; incident only 700s old.
    report, code = oh.build_report(cfg, 1000.0 + 700, alive_fn=lambda: True)
    assert report["wedged"] is True and code == 1
    assert report["flagged_stuck"]["count"] == 0


def test_flagged_stuck_escalated_recent_report_only_no_wake(tmp_path):
    # escalated_recent present but no STUCK open incident → exit 0 (report-only
    # invariant holds; escalation never flips the exit code).
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera11", "panel:SinFermera11", "high", "needs PC",
            fixable=False, novel=True, now=1000.0)
    tr.escalate("panel:SinFermera11", now=1500.0)
    tr.close()
    _beacon(tmp_path, 1560.0)
    report, code = oh.build_report(cfg, 1560.0, alive_fn=lambda: True)
    assert report["escalated_recent"]["bots"] == ["SinFermera11"]
    assert report["flagged_stuck"]["count"] == 0
    assert code == 0 and report["healthy"] is True


def test_flagged_stuck_needs_human_report_only_no_wake(tmp_path):
    # A parked panel (needs_human) present but no stuck open incident → exit 0.
    cfg = _cfg(tmp_path)
    tr = IncidentTracker(cfg.db_path)
    tr.open("panel", "SinFermera14", "panel:SinFermera14", "high", "PC OFF",
            fixable=False, novel=True, now=1000.0)
    iid = tr.open_for_bot("panel", "SinFermera14")["id"]
    tr.park_by_id(iid, now=2000.0)
    tr.close()
    now = 2000.0 + 48 * 3600.0
    _beacon(tmp_path, now)
    report, code = oh.build_report(cfg, now, alive_fn=lambda: True)
    assert report["needs_human"]["bots"] == ["SinFermera14"]
    assert report["flagged_stuck"]["count"] == 0
    assert code == 0 and report["healthy"] is True


def test_flagged_stuck_shape_and_flagged_unchanged(tmp_path):
    # flagged_stuck has the documented shape; the full `flagged` set is present
    # and unchanged (count/bots), and the whole report stays JSON-serialisable.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)     # stuck
    _open_flagged(cfg.db_path, "SinFermera8", now=1000.0 + 19 * 60)  # young
    _beacon(tmp_path, 1000.0 + 20 * 60)
    report, code = oh.build_report(cfg, 1000.0 + 20 * 60, alive_fn=lambda: True)
    # flagged carries BOTH open novel incidents, unchanged.
    assert report["flagged"]["count"] == 2
    assert sorted(report["flagged"]["bots"]) == ["SinFermera7", "SinFermera8"]
    # flagged_stuck only the one over threshold.
    fs = report["flagged_stuck"]
    assert set(fs.keys()) == {"count", "bots", "stale"}
    assert fs["count"] == 1 and fs["bots"] == ["SinFermera7"]
    assert isinstance(fs["stale"], list) and len(fs["stale"]) == 1
    assert set(fs["stale"][0].keys()) == {"bot", "open_min"}
    assert fs["stale"][0]["open_min"] == 20
    assert code == 1
    import json as _json
    _json.dumps(report)


def test_flagged_stuck_env_override_flips_stuck(tmp_path, monkeypatch):
    # Lowering OVERSEER_STUCK_MIN to 1 makes a 5-min incident stuck (exit 1);
    # the default 12 would have left it in-flight (exit 0).
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1000.0 + 5 * 60)
    monkeypatch.setenv("OVERSEER_STUCK_MIN", "1")
    report, code = oh.build_report(cfg, 1000.0 + 5 * 60, alive_fn=lambda: True)
    assert code == 1 and report["flagged_stuck"]["count"] == 1
    assert report["flagged_stuck"]["stale"][0]["open_min"] == 5


def test_flagged_stuck_default_threshold_is_12(tmp_path):
    # Sanity-check the module default: an 11-min incident is in-flight, a 13-min
    # incident is stuck, with no env override set.
    cfg = _cfg(tmp_path)
    _open_flagged(cfg.db_path, "SinFermera7", now=1000.0)
    _beacon(tmp_path, 1000.0 + 13 * 60)
    r11, c11 = oh.build_report(cfg, 1000.0 + 11 * 60, alive_fn=lambda: True)
    assert c11 == 0 and r11["flagged_stuck"]["count"] == 0
    r13, c13 = oh.build_report(cfg, 1000.0 + 13 * 60, alive_fn=lambda: True)
    assert c13 == 1 and r13["flagged_stuck"]["count"] == 1


def test_flagged_stuck_bad_db_degrades(tmp_path):
    # A bad/empty DB path must degrade to the empty shape like its siblings
    # (_flagged/_parked) — count 0, empty lists, never crash. (An `error` key is
    # only present when the IncidentTracker actually raises.)
    out = oh._flagged_stuck(str(tmp_path / "missing-dir" / "x.db"), 600)
    assert out["count"] == 0 and out["bots"] == [] and out["stale"] == []


def test_flagged_stuck_db_failure_carries_error(tmp_path, monkeypatch):
    # When IncidentTracker genuinely raises, the helper degrades with an `error`
    # key (mirrors the sibling try/except branches) instead of crashing the timer.
    def _boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(oh, "IncidentTracker", _boom)
    out = oh._flagged_stuck(str(tmp_path / "i.db"), 600)
    assert out == {"count": 0, "bots": [], "stale": [], "error": "db exploded"}
