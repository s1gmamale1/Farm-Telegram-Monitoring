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


# --- inode change (file rotation) -------------------------------------------

def test_inode_change_resets_and_re_reads(tmp_path):
    """When the log file is rotated (new inode), the monitor reads from the start."""
    mon, log_dir = _monitor(tmp_path)
    log = log_dir / "bot.log"
    log.write_text("ERROR first\n", encoding="utf-8")
    assert len(mon.poll()) == 1

    # Replace the file entirely (new inode, e.g. via logrotate).
    log.unlink()
    log.write_text("ERROR fresh after rotation\n", encoding="utf-8")
    incidents = mon.poll()
    assert len(incidents) == 1
    assert "fresh after rotation" in incidents[0]["text"]


# --- multiple tracebacks in the same read -----------------------------------

def test_multiple_tracebacks_in_single_poll(tmp_path):
    """Two back-to-back tracebacks in the same read must be reported as two incidents."""
    mon, log_dir = _monitor(tmp_path)
    tb1 = (
        "Traceback (most recent call last):\n"
        '  File "bot.py", line 1, in <module>\n'
        "ValueError: first\n"
    )
    tb2 = (
        "Traceback (most recent call last):\n"
        '  File "bot.py", line 2, in <module>\n'
        "KeyError: second\n"
    )
    (log_dir / "multi.log").write_text(tb1 + tb2, encoding="utf-8")
    incidents = mon.poll()
    assert len(incidents) == 2
    texts = [i["text"] for i in incidents]
    assert any("ValueError" in t for t in texts)
    assert any("KeyError" in t for t in texts)


# --- flush_idle_seconds: stale in-progress traceback -------------------------

def test_flush_idle_traceback_after_quiet_period(tmp_path):
    """A traceback that spans the file boundary (last lines in the file) should
    be emitted once the idle threshold passes."""
    import time

    mon, log_dir = _monitor(tmp_path)
    # Write an incomplete traceback (no exception summary line).
    partial = (
        "Traceback (most recent call last):\n"
        '  File "bot.py", line 10, in run\n'
    )
    (log_dir / "bot.log").write_text(partial, encoding="utf-8")
    # First poll starts accumulating the traceback but doesn't emit yet.
    assert mon.poll() == []

    # Simulate idle time past the flush_idle_seconds threshold.
    state = list(mon.states.values())[0]
    state.last_activity = time.monotonic() - 10.0  # well past 5s flush_idle

    incidents = mon.poll()
    assert len(incidents) == 1
    assert "Traceback" in incidents[0]["text"]


# --- normalize_error edge cases ---------------------------------------------

def test_normalize_error_removes_hex_addresses():
    out = normalize_error("at 0xDEAD1234 in malloc")
    assert "<HEX>" in out
    assert "0xDEAD1234" not in out


def test_normalize_error_collapses_integer_in_message():
    out = normalize_error("retry attempt 42 of 100")
    assert "42" not in out
    assert "<N>" in out


# --- FATAL / Unhandled error patterns ---------------------------------------

def test_fatal_line_is_an_incident(tmp_path):
    """A FATAL-level log line must be treated as an incident."""
    mon, log_dir = _monitor(tmp_path)
    (log_dir / "bot.log").write_text("2024-01-01 10:00:00 FATAL database gone\n", encoding="utf-8")
    incidents = mon.poll()
    assert len(incidents) == 1
    assert "FATAL" in incidents[0]["text"]


def test_unhandled_error_line_is_an_incident(tmp_path):
    """Lines containing 'Unhandled error' must trigger an incident."""
    mon, log_dir = _monitor(tmp_path)
    (log_dir / "bot.log").write_text("Unhandled error in asyncio task\n", encoding="utf-8")
    incidents = mon.poll()
    assert len(incidents) == 1
    assert "Unhandled error" in incidents[0]["text"]


# --- _load_offsets resilience: corrupt JSON ---------------------------------

def test_load_offsets_corrupt_json_is_ignored(tmp_path):
    """A corrupt offsets file must not crash the monitor; it starts from zero."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    offsets = tmp_path / "data" / "offsets.json"
    offsets.parent.mkdir(parents=True)
    offsets.write_text("{not valid json", encoding="utf-8")

    mon = LogMonitor(str(log_dir), str(offsets), flush_idle_seconds=5.0)
    # Should be instantiatable and poll-able without error.
    assert mon.poll() == []


# --- _bot_name: path without an extension -----------------------------------

def test_bot_name_no_extension(tmp_path):
    mon, log_dir = _monitor(tmp_path)
    # A file without an extension should still yield the bare filename.
    name = LogMonitor._bot_name(str(log_dir / "SinFermera3"))
    assert name == "SinFermera3"


# --- normalize_error with no volatile parts: passes through -----------------

def test_normalize_error_clean_text_unchanged():
    text = "ValueError: boom"
    assert normalize_error(text) == text
