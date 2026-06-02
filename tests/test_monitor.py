"""Tests for watcherdog.monitor — normalization, hashing, and LogMonitor."""

from __future__ import annotations

from watcherdog.monitor import LogMonitor, error_hash, normalize_error

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "bot.py", line 42, in <module>\n'
    '    raise ValueError("boom")\n'
    "ValueError: boom\n"
)


# --- normalize_error / error_hash ------------------------------------------

def test_normalize_strips_volatile_fields():
    out = normalize_error("2024-01-02 03:04:05 line 99 at 0xdeadbeef code 7")
    assert "<TS>" in out
    assert "line <N>" in out
    assert "<HEX>" in out
    assert "7" not in out  # bare integer collapsed to <N>


def test_same_error_different_timestamp_hashes_equal():
    a = "2024-01-01 00:00:00 ERROR ValueError at line 10"
    b = "2025-12-31 23:59:59 ERROR ValueError at line 873"
    assert error_hash(a) == error_hash(b)


def test_different_errors_hash_differently():
    assert error_hash("ValueError: boom") != error_hash("KeyError: nope")


# --- LogMonitor -------------------------------------------------------------

def _monitor(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    offsets = tmp_path / "data" / "offsets.json"
    return LogMonitor(str(log_dir), str(offsets), flush_idle_seconds=5.0), log_dir


def test_poll_detects_complete_traceback(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    (log_dir / "SinFermera3.log").write_text(TRACEBACK, encoding="utf-8")

    incidents = mon.poll()
    assert len(incidents) == 1
    assert incidents[0]["bot"] == "SinFermera3"
    assert "ValueError: boom" in incidents[0]["text"]


def test_poll_detects_standalone_error_line(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    (log_dir / "bot.log").write_text("2024-01-01 12:00:00 ERROR something failed\n", encoding="utf-8")

    incidents = mon.poll()
    assert len(incidents) == 1
    assert "ERROR something failed" in incidents[0]["text"]


def test_poll_is_incremental_and_does_not_replay(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    log = log_dir / "bot.log"
    log.write_text("ERROR first\n", encoding="utf-8")

    first = mon.poll()
    assert len(first) == 1

    # No new content -> nothing returned the second time.
    assert mon.poll() == []

    # Append a new error -> only the new one comes back.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("ERROR second\n")
    third = mon.poll()
    assert len(third) == 1
    assert "second" in third[0]["text"]


def test_offsets_persist_across_monitor_instances(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    log = log_dir / "bot.log"
    log.write_text("ERROR boom\n", encoding="utf-8")
    assert len(mon.poll()) == 1

    # A fresh monitor pointed at the same offsets file must not re-report.
    mon2 = LogMonitor(str(log_dir), mon.offsets_path)
    assert mon2.poll() == []


def test_truncation_resets_offset(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    log = log_dir / "bot.log"
    log.write_text("ERROR one\nERROR two\n", encoding="utf-8")
    assert len(mon.poll()) == 2

    # Rotated/truncated: file shrinks -> monitor reads from the start again.
    log.write_text("ERROR fresh\n", encoding="utf-8")
    incidents = mon.poll()
    assert len(incidents) == 1
    assert "fresh" in incidents[0]["text"]


def test_no_logs_returns_empty(tmp_path):
    mon, _ = _monitor(tmp_path)
    assert mon.poll() == []
