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
