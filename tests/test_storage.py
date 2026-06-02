"""Tests for watcherdog.storage.IncidentStore (SQLite history)."""

from __future__ import annotations

import pytest

from watcherdog.storage import IncidentStore

ANALYSIS = {
    "severity": "high",
    "summary": "boom",
    "root_cause": "cause",
    "fix": "fix it",
}


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(str(tmp_path / "data" / "incidents.db"))
    yield s
    s.close()


def test_record_returns_rowid(store):
    rid = store.record("bot", "high", ANALYSIS, "hash1", "raw", True, ts=1000.0)
    assert isinstance(rid, int) and rid > 0


def test_last_seen_unknown_hash_is_none(store):
    assert store.last_seen("never-seen") is None


def test_last_seen_returns_most_recent_ts(store):
    store.record("bot", "high", ANALYSIS, "dup", "raw", True, ts=1000.0)
    store.record("bot", "high", ANALYSIS, "dup", "raw", True, ts=2000.0)
    assert store.last_seen("dup") == 2000.0


def test_recent_orders_newest_first_and_limits(store):
    for i in range(5):
        store.record("bot", "high", ANALYSIS, f"h{i}", "raw", True, ts=float(i))
    recent = store.recent(limit=3)
    assert len(recent) == 3
    assert [r["ts"] for r in recent] == [4.0, 3.0, 2.0]


def test_record_persists_analysis_fields(store):
    store.record("SinFermera3", "critical", ANALYSIS, "h", "raw excerpt", False, ts=5.0)
    row = store.recent(limit=1)[0]
    assert row["bot"] == "SinFermera3"
    assert row["severity"] == "critical"
    assert row["summary"] == "boom"
    assert row["notified"] == 0


def test_record_handles_missing_analysis(store):
    rid = store.record("bot", "high", None, "h", "raw", True, ts=1.0)
    assert rid > 0
    row = store.recent(limit=1)[0]
    assert row["summary"] == ""


def test_long_excerpt_truncated_to_4000(store):
    store.record("bot", "high", ANALYSIS, "h", "y" * 9000, True, ts=1.0)
    row = store.recent(limit=1)[0]
    assert len(row["raw_excerpt"]) == 4000
