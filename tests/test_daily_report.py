"""Tests for watcherdog.daily_report — the AI-fix log + end-of-day report (skill 2)."""

from __future__ import annotations

import json

from watcherdog import daily_report


# --- record / load ----------------------------------------------------------

def test_record_appends_json_line(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="Panel#3", error="proxy timeout",
                        fix="restarted panel", result="ok", ts="2026-06-02T14:03:00")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {"ts": "2026-06-02T14:03:00", "panel": "Panel#3",
                   "error": "proxy timeout", "fix": "restarted panel", "result": "ok"}


# --- summary_since (hourly fix block, Phase 4) ------------------------------

def test_summary_since_filters_and_groups(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="SF1", error="old", fix="x", ts="2026-06-04T10:00:00")
    daily_report.record(str(p), panel="SF3", error="proxy timeout", fix="relaunch",
                        ts="2026-06-04T12:05:00")
    daily_report.record(str(p), panel="SF3", error="proxy timeout", fix="relaunch",
                        ts="2026-06-04T12:40:00")
    daily_report.record(str(p), panel="SF7", error="CS frozen", fix="kill+start",
                        result="failed", ts="2026-06-04T12:50:00")
    line = daily_report.summary_since(str(p), "2026-06-04T12:00:00")
    assert line.startswith("🔧 Fixed last hour:")
    assert "SF3 proxy timeout ×2" in line   # grouped + counted
    assert "SF7 CS frozen ⚠️" in line        # failed flagged
    assert "SF1" not in line                  # before the window, excluded


def test_summary_since_none_when_empty(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="SF1", error="e", fix="f", ts="2026-06-04T09:00:00")
    assert daily_report.summary_since(str(p), "2026-06-04T12:00:00") is None
    assert daily_report.summary_since(str(tmp_path / "missing.jsonl"), "2026-06-04T12:00:00") is None


def test_record_defaults_timestamp(tmp_path):
    p = tmp_path / "daily.jsonl"
    entry = daily_report.record(str(p), panel="P1", error="e", fix="f")
    assert entry["ts"]  # auto-stamped
    assert entry["result"] == "ok"


def test_record_creates_missing_dir(tmp_path):
    p = tmp_path / "nested" / "daily.jsonl"
    daily_report.record(str(p), panel="P1", error="e", fix="f")
    assert p.exists()


def test_load_entries_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "daily.jsonl"
    p.write_text('{"panel":"P1","error":"e","fix":"f","result":"ok"}\n'
                 "\n"
                 "not json\n"
                 '{"panel":"P2","error":"x","fix":"y","result":"ok"}\n',
                 encoding="utf-8")
    entries = daily_report.load_entries(str(p))
    assert [e["panel"] for e in entries] == ["P1", "P2"]


def test_load_entries_missing_returns_empty(tmp_path):
    assert daily_report.load_entries(str(tmp_path / "nope.jsonl")) == []


# --- has_pending ------------------------------------------------------------

def test_has_pending(tmp_path):
    p = tmp_path / "daily.jsonl"
    assert daily_report.has_pending(str(p)) is False
    daily_report.record(str(p), panel="P1", error="e", fix="f")
    assert daily_report.has_pending(str(p)) is True


# --- format_report ----------------------------------------------------------

def test_format_report_empty_is_none():
    assert daily_report.format_report([]) is None


def test_format_report_groups_and_counts():
    entries = [
        {"panel": "Panel#1", "error": "proxy timeout", "fix": "restarted", "result": "ok"},
        {"panel": "Panel#1", "error": "proxy timeout", "fix": "restarted", "result": "ok"},
        {"panel": "Panel#3", "error": "Steam stuck", "fix": "killed & relaunched", "result": "ok"},
    ]
    out = daily_report.format_report(entries)
    assert "🐕 Today — 3 errors auto-fixed" in out
    assert "• Panel#1 ×2 — proxy timeout → restarted, ok" in out
    assert "• Panel#3 ×1 — Steam stuck → killed & relaunched, ok" in out
    assert "(file cleared)" in out


def test_format_report_singular_and_no_clear_footer():
    entries = [{"panel": "P1", "error": "e", "fix": "f", "result": "ok"}]
    out = daily_report.format_report(entries, cleared=False)
    assert "1 error auto-fixed" in out  # singular
    assert "(file cleared)" not in out


# --- build_report / clear_log -----------------------------------------------

def test_build_report_and_clear_roundtrip(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="P1", error="e", fix="f")
    assert daily_report.build_report(str(p)) is not None
    daily_report.clear_log(str(p))
    assert daily_report.has_pending(str(p)) is False
    assert daily_report.build_report(str(p)) is None


def test_clear_log_missing_file_is_noop(tmp_path):
    daily_report.clear_log(str(tmp_path / "nope.jsonl"))  # must not raise


# --- entries_since ----------------------------------------------------------

def test_entries_since_filters_by_timestamp(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="P1", error="old", fix="f", ts="2026-06-04T10:00:00")
    daily_report.record(str(p), panel="P2", error="new", fix="f", ts="2026-06-04T12:00:00")
    entries = daily_report.entries_since(str(p), "2026-06-04T11:00:00")
    assert len(entries) == 1
    assert entries[0]["panel"] == "P2"


def test_entries_since_empty_since_returns_all(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="P1", error="e", fix="f", ts="2026-06-04T09:00:00")
    daily_report.record(str(p), panel="P2", error="e", fix="f", ts="2026-06-04T10:00:00")
    entries = daily_report.entries_since(str(p), "")
    assert len(entries) == 2


def test_entries_since_missing_file(tmp_path):
    assert daily_report.entries_since(str(tmp_path / "nope.jsonl"), "2026-06-04T00:00:00") == []


# --- summary_since: max_items overflow --------------------------------------

def test_summary_since_truncates_at_max_items(tmp_path):
    p = tmp_path / "daily.jsonl"
    since = "2026-06-04T12:00:00"
    for i in range(20):
        daily_report.record(str(p), panel=f"SF{i}", error=f"err{i}", fix="f",
                            ts="2026-06-04T12:30:00")
    line = daily_report.summary_since(str(p), since, max_items=5)
    assert line is not None
    assert "+15 more" in line  # 20 - 5 overflow items


# --- build_report: cleared=False omits footer --------------------------------

def test_build_report_cleared_false_has_no_footer(tmp_path):
    p = tmp_path / "daily.jsonl"
    daily_report.record(str(p), panel="P1", error="e", fix="f")
    out = daily_report.build_report(str(p), cleared=False)
    assert out is not None
    assert "(file cleared)" not in out
