"""Tests for watcherdog.heartbeat — silence/recovery detection."""

from __future__ import annotations

from watcherdog.heartbeat import HeartbeatMonitor

THRESHOLD = 1800.0  # 30 minutes
START = 1_000_000.0


def _monitor(tmp_path, expected=None):
    path = tmp_path / "data" / "heartbeats.json"
    return HeartbeatMonitor(str(path), THRESHOLD, START, expected_bots=expected)


def test_record_new_bot_is_not_a_recovery(tmp_path):
    mon = _monitor(tmp_path)
    assert mon.record("SinFermera3", START + 10) is False
    assert mon.last_seen["SinFermera3"] == START + 10


def test_grace_period_suppresses_early_alerts(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    # Still inside the first threshold window since start -> no alerts yet.
    assert mon.check(START + THRESHOLD - 1) == []


def test_silence_detected_after_grace(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    # Well past the grace window, and the bot hasn't been seen since START.
    now = START + THRESHOLD + 100
    silent = mon.check(now)
    assert len(silent) == 1
    bot, silent_for = silent[0]
    assert bot == "SinFermera3"
    assert silent_for == now - START


def test_silence_alerted_only_once(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    now = START + THRESHOLD + 100
    assert len(mon.check(now)) == 1
    # Already flagged -> not re-reported on the next check.
    assert mon.check(now + 50) == []


def test_recovery_after_silence(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    now = START + THRESHOLD + 100
    mon.check(now)  # flags SinFermera3 silent
    # The bot posts again -> record() reports a recovery.
    assert mon.record("SinFermera3", now + 10) is True
    assert "SinFermera3" not in mon.alerted


def test_active_bot_never_flagged(tmp_path):
    mon = _monitor(tmp_path)
    now = START + THRESHOLD + 100
    mon.record("SinFermera3", now - 10)  # recent heartbeat
    assert mon.check(now) == []


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "data" / "heartbeats.json"
    mon = HeartbeatMonitor(str(path), THRESHOLD, START)
    mon.record("SinFermera3", START + 10)

    # New instance loads the known bot (clock reset to its own start_time).
    later_start = START + 5000
    mon2 = HeartbeatMonitor(str(path), THRESHOLD, later_start)
    assert "SinFermera3" in mon2.last_seen
    assert mon2.last_seen["SinFermera3"] == later_start
