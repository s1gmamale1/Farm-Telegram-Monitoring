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
