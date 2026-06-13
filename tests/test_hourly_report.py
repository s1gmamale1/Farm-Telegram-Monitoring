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


def _fleet_entry(status, age, name, code="", detail=""):
    return {"status": status, "age_min": age, "name": name,
            "reason_code": code, "reason_detail": detail, "pc": "?"}


def test_build_all_green_oneliner():
    fleet = {n: _fleet_entry("✅ farming", 2, f"SinFermera{n}") for n in range(1, 25)}
    text, state = hr.build(fleet, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — ✅ all 24 farming · 🔧 no fixes needed"
    assert state["last_snapshot"]["1"] == "✅"
    assert state["last_sent_iso"] == "2026-06-13T03:00:00"


def test_build_all_green_with_fixes_shows_fix_clause():
    fleet = {n: _fleet_entry("✅ farming", 2, f"SinFermera{n}") for n in range(1, 4)}
    text, _ = hr.build(fleet, [], "🔧 Fixed last hour: SF1 proxy", {},
                       datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — ✅ all 3 farming · 🔧 Fixed last hour: SF1 proxy"


def test_build_empty_roster():
    text, _ = hr.build({}, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert text == "🐕 03:00 — no panels in watch"


def test_build_layered_sections_and_ordering():
    fleet = {
        1: _fleet_entry("🔴 needs attention", 1, "SinFermera1", "error", "error"),
        10: _fleet_entry("🔴 needs attention", 24, "SinFermera10", "accounts", "accounts 2/4"),
        2: _fleet_entry("⚠️ quiet", 75, "SinFermera2", "quiet", ""),
        7: _fleet_entry("⚠️ quiet", 2, "SinFermera7", "quiet", ""),
        3: _fleet_entry("✅ farming", 2, "SinFermera3"),
        5: _fleet_entry("✅ farming", 2, "SinFermera5"),
    }
    text, _ = hr.build(fleet, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert "NEEDS ATTENTION" in text
    assert "🔴 SF10 — accounts 2/4 · 24m" in text
    assert "🔴 SF1 — error · 1m" in text
    assert text.index("🔴 SF10 —") < text.index("🔴 SF1 —")
    assert "⚠️ SF2 75m · SF7 2m" in text
    assert "✅ FARMING (2): SF3 SF5" in text
    assert "🔧 No fixes needed last hour." in text


def test_build_joins_incident_action():
    fleet = {
        10: _fleet_entry("🔴 needs attention", 24, "SinFermera10", "accounts", "accounts 2/4"),
    }
    incidents = [{"bot": "SinFermera10", "novel": 0, "fix_attempted": "relaunch",
                  "fix_retries": 1, "severity": "high"}]
    text, _ = hr.build(fleet, incidents, None, {}, datetime(2026, 6, 13, 3, 0))
    assert "🔴 SF10 — accounts 2/4 · 24m · relaunch ×2" in text


def test_build_new_and_recovered_markers():
    fleet = {
        9: _fleet_entry("🔴 needs attention", 5, "SinFermera9", "error", "err"),
        7: _fleet_entry("✅ farming", 2, "SinFermera7"),
    }
    prev = {"last_snapshot": {"9": "✅", "7": "🔴"},
            "last_sent_iso": "2026-06-13T02:00:00"}
    text, _ = hr.build(fleet, [], None, prev, datetime(2026, 6, 13, 3, 0))
    assert "🔴 SF9 🆕" in text
    assert "recovered since 02:00: SF7" in text


def test_build_join_with_real_incident_tracker(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker

    db = tmp_path / "inc.db"
    tr = IncidentTracker(str(db))
    tr.open("panel", "SinFermera15", "panel:SinFermera15", "high",
            "screen grab failed", fixable=False, novel=True)
    incidents = tr.open_list()

    fleet = {15: _fleet_entry("🔴 needs attention", 11, "SinFermera15",
                              "error", "error creating screenshot")}
    text, _ = hr.build(fleet, incidents, None, {}, datetime(2026, 6, 13, 3, 0))
    assert "cold-cased, needs PC" in text


def test_build_first_run_suppresses_new_markers():
    # No prior snapshot → no baseline → nothing is "🆕" even though panels are flagged.
    fleet = {
        1: _fleet_entry("🔴 needs attention", 5, "SinFermera1", "error", "err"),
        2: _fleet_entry("⚠️ quiet", 80, "SinFermera2", "quiet", ""),
    }
    text, _ = hr.build(fleet, [], None, {}, datetime(2026, 6, 13, 3, 0))
    assert "🆕" not in text


def test_hourly_state_roundtrip(tmp_path):
    from watcherdog import mcp_watcher

    class _Cfg:
        db_path = str(tmp_path / "incidents.db")

    cfg = _Cfg()
    assert mcp_watcher._load_hourly_state(cfg) == {}  # absent file → {}

    state = {"last_hour": "2026-06-13 03",
             "last_sent_iso": "2026-06-13T03:00:00",
             "last_snapshot": {"1": "🔴", "2": "✅"}}
    mcp_watcher._save_hourly_state(cfg, state)
    loaded = mcp_watcher._load_hourly_state(cfg)
    assert loaded["last_snapshot"] == {"1": "🔴", "2": "✅"}
    assert loaded["last_sent_iso"] == "2026-06-13T03:00:00"

    # _hourly_already_sent still reads last_hour from the richer file
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-13 03") is True
    assert mcp_watcher._hourly_already_sent(cfg, "2026-06-13 04") is False
