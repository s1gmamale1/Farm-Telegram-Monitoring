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


def test_recurring_flags_repeated_hash(store):
    now = 10_000.0
    for i in range(3):
        store.record("SinFermera5", "high", {"summary": "proxy timeout"},
                     "DUP", "proxy timeout", False, ts=now - i * 60)
    groups = store.recurring(3600, 3, now=now)
    assert len(groups) == 1
    g = groups[0]
    assert g["raw_hash"] == "DUP"
    assert g["count"] == 3
    assert g["bots"] == ["SinFermera5"]
    assert g["summary"] == "proxy timeout"   # from the latest incident


def test_recurring_ignores_below_min_count(store):
    now = 10_000.0
    store.record("b", "high", ANALYSIS, "h", "raw", False, ts=now - 10)
    store.record("b", "high", ANALYSIS, "h", "raw", False, ts=now - 20)
    assert store.recurring(3600, 3, now=now) == []  # only 2 < min_count 3


def test_recurring_respects_window(store):
    now = 10_000.0
    for i in range(4):
        store.record("b", "high", ANALYSIS, "h", "raw", False, ts=now - i * 60)
    # All four are inside the last hour...
    assert store.recurring(3600, 3, now=now)
    # ...but none within the last 30s.
    assert store.recurring(30, 3, now=now) == []


def test_recurring_collects_distinct_bots(store):
    now = 10_000.0
    for bot in ("SinFermera1", "SinFermera2", "SinFermera1"):
        store.record(bot, "high", ANALYSIS, "shared", "raw", False, ts=now - 5)
    g = store.recurring(3600, 3, now=now)[0]
    assert sorted(g["bots"]) == ["SinFermera1", "SinFermera2"]
    assert g["count"] == 3


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


# --- notified field stored correctly as 1 / 0 --------------------------------

def test_record_notified_true_stores_1(store):
    store.record("bot", "high", ANALYSIS, "h1", "raw", True, ts=1.0)
    row = store.recent(limit=1)[0]
    assert row["notified"] == 1


def test_record_notified_false_stores_0(store):
    store.record("bot", "high", ANALYSIS, "h2", "raw", False, ts=1.0)
    row = store.recent(limit=1)[0]
    assert row["notified"] == 0


# --- recurring: multiple distinct hashes, ordered by count ------------------

def test_recurring_multiple_hashes_ordered_by_frequency(store):
    now = 10_000.0
    # Hash "A" appears 5 times; hash "B" appears 3 times.
    for i in range(5):
        store.record("b", "high", {"summary": "A error"}, "A", "raw-a", False, ts=now - i)
    for i in range(3):
        store.record("b", "high", {"summary": "B error"}, "B", "raw-b", False, ts=now - i)
    groups = store.recurring(3600, 3, now=now)
    assert len(groups) >= 2
    assert groups[0]["raw_hash"] == "A"   # most frequent first
    assert groups[0]["count"] == 5
    assert groups[1]["raw_hash"] == "B"
    assert groups[1]["count"] == 3


# --- recent: limit=0 returns empty ------------------------------------------

def test_recent_limit_zero_returns_empty(store):
    store.record("bot", "high", ANALYSIS, "h", "raw", True, ts=1.0)
    assert store.recent(limit=0) == []


# --- close is idempotent (no raise on double-close) -------------------------

def test_close_is_idempotent(tmp_path):
    s = IncidentStore(str(tmp_path / "data" / "incidents.db"))
    s.close()
    s.close()  # must not raise


# --- schema is idempotent: second IncidentStore on same path ----------------

def test_schema_is_idempotent(tmp_path):
    """Opening two IncidentStore instances on the same DB path must not error."""
    path = str(tmp_path / "data" / "incidents.db")
    s1 = IncidentStore(path)
    s1.record("bot", "high", ANALYSIS, "h1", "raw", True, ts=1.0)
    s1.close()

    s2 = IncidentStore(path)
    rows = s2.recent(limit=10)
    assert len(rows) == 1
    s2.close()


# --- recurring() with now=None uses current wall-clock ----------------------

def test_recurring_now_none_uses_current_time(store):
    """recurring(now=None) must default to time.time() without crashing."""
    store.record("bot", "high", ANALYSIS, "H", "raw", False)
    # With now=None the query runs against the real clock; we just verify no crash.
    groups = store.recurring(3600, 1, now=None)
    assert isinstance(groups, list)


# --- recent: returns dict rows, not sqlite.Row objects ----------------------

def test_recent_returns_plain_dicts(store):
    store.record("bot", "high", ANALYSIS, "h", "raw", True, ts=1.0)
    rows = store.recent(limit=1)
    assert isinstance(rows[0], dict)
    assert "bot" in rows[0]
