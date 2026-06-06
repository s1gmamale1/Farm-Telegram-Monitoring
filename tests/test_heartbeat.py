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


# --- multiple bots go silent simultaneously ---------------------------------

def test_multiple_bots_silenced_in_one_check(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera1", "SinFermera2", "SinFermera3"])
    now = START + THRESHOLD + 200
    silent = mon.check(now)
    names = [bot for bot, _ in silent]
    assert "SinFermera1" in names
    assert "SinFermera2" in names
    assert "SinFermera3" in names
    assert len(silent) == 3


def test_only_silent_bots_are_reported(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera1", "SinFermera2"])
    now = START + THRESHOLD + 200
    # Give SinFermera2 a recent heartbeat.
    mon.record("SinFermera2", now - 10)
    silent = mon.check(now)
    names = [bot for bot, _ in silent]
    assert "SinFermera1" in names
    assert "SinFermera2" not in names


# --- new bot discovery saves state ------------------------------------------

def test_new_bot_triggers_save(tmp_path):
    path = tmp_path / "data" / "heartbeats.json"
    mon = HeartbeatMonitor(str(path), THRESHOLD, START)
    mon.record("NewBot", START + 5)  # new bot: triggers save
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert "NewBot" in data.get("bots", [])


# --- already-alerted bot correctly cleared on recovery ---------------------

def test_recovery_clears_alerted_set(tmp_path):
    mon = _monitor(tmp_path, expected=["SinFermera9"])
    now = START + THRESHOLD + 100
    mon.check(now)  # flagged silent
    assert "SinFermera9" in mon.alerted
    mon.record("SinFermera9", now + 5)  # recovery
    assert "SinFermera9" not in mon.alerted
    # A subsequent re-silence should re-alert.
    later = now + THRESHOLD + 100
    silent = mon.check(later)
    assert any(b == "SinFermera9" for b, _ in silent)


# --- corrupt JSON file on load: graceful fallback ---------------------------

def test_load_corrupt_json_falls_back_to_empty(tmp_path):
    """A corrupt/invalid heartbeat file must be tolerated; no bots pre-loaded."""
    path = tmp_path / "data" / "heartbeats.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")

    mon = HeartbeatMonitor(str(path), THRESHOLD, START)
    assert mon.last_seen == {}
    assert mon.alerted == set()


# --- grace period boundary: exactly at threshold ----------------------------

def test_grace_period_at_exact_threshold_still_suppresses(tmp_path):
    """Check at exactly start_time + threshold must not fire (strictly > needed)."""
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    assert mon.check(START + THRESHOLD) == []


# --- expected_bots seeded with start_time, then check just after grace ------

def test_expected_bots_are_seeded_at_start_time(tmp_path):
    """Expected bots must be seeded with start_time, so they fire at start+threshold+ε."""
    mon = _monitor(tmp_path, expected=["SinFermera3"])
    now = START + THRESHOLD + 1
    silent = mon.check(now)
    assert any(b == "SinFermera3" for b, _ in silent)
