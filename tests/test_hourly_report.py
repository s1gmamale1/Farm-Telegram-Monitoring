"""Tests for the deterministic hourly-report builder (pure; no Telethon)."""
from datetime import datetime

from watcherdog import hourly_report as hr


def test_snapshot_maps_botnum_to_emoji():
    fleet = {
        1: {"status": "🔴 needs attention", "age_min": 5, "name": "SinFermera1",
            "reason_code": "error", "reason_detail": "x"},
        3: {"status": "✅ farming", "age_min": 2, "name": "SinFermera3",
            "reason_code": "", "reason_detail": ""},
    }
    snap = hr._snapshot(fleet)
    assert snap == {"1": "🔴", "3": "✅"}


def test_diff_flags_new_and_recovered():
    prev = {"1": "✅", "2": "🔴", "3": "⚠️"}
    cur = {"1": "🔴", "2": "🔴", "3": "✅"}
    new_flagged, recovered = hr._diff(prev, cur)
    assert new_flagged == {"1"}
    assert recovered == [3]
    assert "2" not in new_flagged


def test_diff_absent_prev_counts_as_new():
    new_flagged, recovered = hr._diff({}, {"5": "⚠️"})
    assert new_flagged == {"5"}
    assert recovered == []


def test_gap_line_only_when_over_threshold():
    now = datetime(2026, 6, 13, 3, 0, 0)
    # 4h gap (prev send 2026-06-12 23:00) → line present
    line = hr._gap_line({"last_sent_iso": "2026-06-12T23:00:00"}, now)
    assert line and "gap" in line and "23:00" in line
    # 60 min → no line
    assert hr._gap_line({"last_sent_iso": "2026-06-13T02:00:00"}, now) is None
    # absent / malformed → no line, no crash
    assert hr._gap_line({}, now) is None
    assert hr._gap_line({"last_sent_iso": "not-a-date"}, now) is None


def test_truncate():
    assert hr._truncate("short", 60) == "short"
    long = "x" * 80
    out = hr._truncate(long, 60)
    assert len(out) == 60 and out.endswith("…")


def test_index_incidents_keeps_latest_per_bot():
    incs = [
        {"bot": "SinFermera10", "fix_attempted": "relaunch", "fix_retries": 0,
         "novel": 0, "severity": "high"},
        {"bot": "SinFermera10", "fix_attempted": "relaunch", "fix_retries": 1,
         "novel": 0, "severity": "high"},  # newer (open_list is opened_ts ASC)
    ]
    idx = hr._index_incidents(incs)
    assert idx["SinFermera10"]["fix_retries"] == 1


def test_panel_action_variants():
    assert hr._panel_action(None) == ""
    assert hr._panel_action({"novel": 1}) == "cold-cased, needs PC"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "", "fix_retries": 0}) == "incident open"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "relaunch", "fix_retries": 0}) == "relaunch"
    assert hr._panel_action(
        {"novel": 0, "fix_attempted": "novel-ladder", "fix_retries": 1}
    ) == "novel ladder ×2"


def test_panel_reason_uses_detail_then_falls_back():
    assert hr._panel_reason(
        {"reason_code": "error", "reason_detail": "proxy timeout"}) == "proxy timeout"
    assert hr._panel_reason(
        {"reason_code": "accounts", "reason_detail": "accounts 2/4"}) == "accounts 2/4"
    assert hr._panel_reason(
        {"reason_code": "stale", "reason_detail": ""}) == "stale"
    assert hr._panel_reason(
        {"reason_code": "quiet", "reason_detail": ""}) == "quiet"
