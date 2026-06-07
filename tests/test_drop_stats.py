"""Tests for watcherdog.drop_stats — skill 5 pure helpers (parse/buffer/report/schedule)."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from watcherdog import drop_stats
from watcherdog.config import Config
from watcherdog.drop_sheets import COLUMNS


# --- iso_week / buffer_path -------------------------------------------------

def test_iso_week_format():
    # 2026-06-03 is a Wednesday in ISO week 23.
    assert drop_stats.iso_week(datetime(2026, 6, 3, 0, 0)) == "2026-W23"


def test_iso_week_zero_pads():
    assert drop_stats.iso_week(datetime(2026, 1, 5)) == "2026-W02"


def test_buffer_path_uses_week_filename():
    p = drop_stats.buffer_path("/data/drop_stats", "2026-W23")
    assert p == os.path.join("/data/drop_stats", "2026-W23.json")


# --- panel_label ------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Panel 3", "Panel#3"),
    ("panel-12 (CS2)", "Panel#12"),
    ("Panel#7", "Panel#7"),
    ("no number here", "no number here"),
    ("", "Panel#?"),
])
def test_panel_label(name, expected):
    assert drop_stats.panel_label(name) == expected


# --- parse_drop_stats -------------------------------------------------------

def test_parse_drop_stats_typical():
    text = "Drops this week: 312\nItems: 18\nValue: $45.50"
    parsed = drop_stats.parse_drop_stats(text)
    assert parsed["drops"] == "312"
    assert parsed["items"] == "18"
    assert parsed["value"] == "45.50"


def test_parse_drop_stats_strips_commas_and_inverted_order():
    parsed = drop_stats.parse_drop_stats("1,204 drops · worth $1,050")
    assert parsed["drops"] == "1204"
    assert parsed["value"] == "1050"


def test_parse_drop_stats_empty():
    parsed = drop_stats.parse_drop_stats("")
    assert parsed == {"drops": "", "items": "", "value": "", "notes": ""}


# --- make_row ---------------------------------------------------------------

def test_make_row_has_all_columns():
    row = drop_stats.make_row("2026-W23", "Panel#1",
                              {"drops": "10", "items": "2", "value": "5"},
                              date="2026-06-03")
    assert set(row.keys()) == set(COLUMNS)
    assert row["week"] == "2026-W23"
    assert row["date"] == "2026-06-03"
    assert row["panel"] == "Panel#1"
    assert row["drops"] == "10"


def test_make_row_missing_fields_blank():
    row = drop_stats.make_row("2026-W23", "Panel#2", None, date="2026-06-03")
    assert row["drops"] == "" and row["items"] == "" and row["value"] == ""


# --- write_buffer / load_buffer ---------------------------------------------

def test_write_and_load_buffer_roundtrip(tmp_path):
    path = str(tmp_path / "2026-W23.json")
    rows = [drop_stats.make_row("2026-W23", "Panel#1", {"drops": "5"}, date="2026-06-03")]
    drop_stats.write_buffer(path, "2026-W23", rows, generated="2026-06-03T00:00:00")
    on_disk = json.loads((tmp_path / "2026-W23.json").read_text(encoding="utf-8"))
    assert on_disk["week"] == "2026-W23"
    assert on_disk["generated"] == "2026-06-03T00:00:00"
    assert on_disk["rows"] == rows
    assert drop_stats.load_buffer(path) == on_disk


def test_write_buffer_creates_missing_dir(tmp_path):
    path = str(tmp_path / "nested" / "2026-W23.json")
    drop_stats.write_buffer(path, "2026-W23", [])
    assert os.path.exists(path)


def test_load_buffer_missing_returns_none(tmp_path):
    assert drop_stats.load_buffer(str(tmp_path / "nope.json")) is None


# --- format_report ----------------------------------------------------------

def _rows():
    return [
        drop_stats.make_row("2026-W23", "Panel#1", {"drops": "312", "value": "45"}, date="d"),
        drop_stats.make_row("2026-W23", "Panel#2", {"drops": "280"}, date="d"),
    ]


def test_format_report_pushed():
    out = drop_stats.format_report("2026-W23", _rows(), {"ok": True, "written": 2})
    assert "🐕 Weekly drops — 2026-W23" in out
    assert "• Panel#1 — 312 drops · ~$45" in out
    assert "• Panel#2 — 280 drops" in out
    assert "Total: 592 · saved to Sheets ✅" in out


def test_format_report_not_configured():
    out = drop_stats.format_report("2026-W23", _rows(), {"ok": False, "reason": "not configured"})
    assert "Total: 592 · buffered, no API key yet" in out


def test_format_report_other_failure_shows_reason():
    out = drop_stats.format_report("2026-W23", _rows(),
                                   {"ok": False, "reason": "gspread not installed"})
    assert "buffered (gspread not installed)" in out


def test_format_report_blank_drops_marked_question():
    rows = [drop_stats.make_row("2026-W23", "Panel#9", {}, date="d")]
    out = drop_stats.format_report("2026-W23", rows, {"ok": True})
    assert "• Panel#9 — ? drops" in out
    assert "Total: 0 · saved to Sheets ✅" in out


# --- seconds_until (Wednesday 00:00) ----------------------------------------

def test_seconds_until_next_wednesday_from_monday():
    # Monday 2026-06-01 12:00 -> Wednesday 2026-06-03 00:00 = 1.5 days.
    secs = drop_stats.seconds_until(datetime(2026, 6, 1, 12, 0))
    assert secs == 1.5 * 24 * 3600


def test_seconds_until_on_the_mark_schedules_full_week():
    # Exactly Wednesday 00:00 -> next one is a full week out, never 0.
    secs = drop_stats.seconds_until(datetime(2026, 6, 3, 0, 0))
    assert secs == 7 * 24 * 3600


def test_seconds_until_later_wednesday_rolls_to_next_week():
    # Wednesday 00:01 -> almost a full week (target already passed today).
    secs = drop_stats.seconds_until(datetime(2026, 6, 3, 0, 1))
    assert secs == 7 * 24 * 3600 - 60


# --- collect_week: activity booster runs AFTER drop stats -------------------

def test_collect_week_runs_activity_booster_after_drop_stats(monkeypatch):
    """Operator rule: per panel the activity booster fires after drop stats."""
    import asyncio

    calls = []

    async def fake_stop_farm(client, ent):
        calls.append(("stop_farm", ent))
        return True

    async def fake_request_drop_stats(client, ent, **kw):
        calls.append(("drop_stats", ent))
        return "312 drops"

    async def fake_run_activity_booster(client, ent):
        calls.append(("activity_booster", ent))
        return True

    monkeypatch.setattr(drop_stats, "stop_farm", fake_stop_farm)
    monkeypatch.setattr(drop_stats, "request_drop_stats", fake_request_drop_stats)
    monkeypatch.setattr(drop_stats, "run_activity_booster", fake_run_activity_booster)

    panels = [("Panel 1", "ent1"), ("Panel 2", "ent2")]
    rows = asyncio.run(
        drop_stats.collect_week(None, Config({}), panels, week="2026-W23", date="2026-06-03")
    )

    # Two panels, three presses each, in the right order.
    assert calls == [
        ("stop_farm", "ent1"), ("drop_stats", "ent1"), ("activity_booster", "ent1"),
        ("stop_farm", "ent2"), ("drop_stats", "ent2"), ("activity_booster", "ent2"),
    ]
    # For each panel, drop_stats must come before its activity_booster.
    for ent in ("ent1", "ent2"):
        order = [name for name, e in calls if e == ent]
        assert order.index("drop_stats") < order.index("activity_booster")
    assert len(rows) == 2


def test_collect_week_dry_run_presses_nothing(monkeypatch):
    """deliver=False must press NO buttons (not even Kill all) — just dry-run rows."""
    import asyncio

    calls = []

    async def boom_stop(client, ent):
        calls.append("stop_farm")

    async def boom_drops(client, ent, **kw):
        calls.append("drop_stats")
        return "x"

    async def boom_boost(client, ent):
        calls.append("activity_booster")

    monkeypatch.setattr(drop_stats, "stop_farm", boom_stop)
    monkeypatch.setattr(drop_stats, "request_drop_stats", boom_drops)
    monkeypatch.setattr(drop_stats, "run_activity_booster", boom_boost)

    panels = [("Panel 1", "ent1"), ("Panel 2", "ent2")]
    rows = asyncio.run(
        drop_stats.collect_week(None, Config({}), panels, week="2026-W23",
                                date="2026-06-03", deliver=False)
    )
    assert calls == []                       # nothing pressed
    assert len(rows) == 2
    assert all(r["notes"] == "dry-run" for r in rows)


# --- env bridge + push (drop_sheets stays as-is) ----------------------------

def test_push_to_sheets_not_configured_keeps_buffer(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    cfg = Config({})  # no GSHEETS_* set
    result = drop_stats.push_to_sheets(cfg, [{"week": "2026-W23", "panel": "Panel#1"}])
    assert result == {"ok": False, "reason": "not configured"}


def test_bridge_sheets_env_mirrors_config(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    creds = tmp_path / "credentials.json"
    creds.write_text("{}", encoding="utf-8")
    cfg = Config({
        "GSHEETS_CREDENTIALS": str(creds),
        "GSHEETS_SHEET_ID": "sheet-123",
        "GSHEETS_TAB": "DropStats",
    })
    drop_stats._bridge_sheets_env(cfg)
    assert os.environ["GSHEETS_CREDENTIALS"] == str(creds)
    assert os.environ["GSHEETS_SHEET_ID"] == "sheet-123"
    assert os.environ["GSHEETS_TAB"] == "DropStats"


# --- drop_sheets.is_configured edge cases -----------------------------------

from watcherdog import drop_sheets


def test_drop_sheets_not_configured_when_no_sheet_id(monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GSHEETS_CREDENTIALS", str(creds))
    monkeypatch.delenv("GSHEETS_SHEET_ID", raising=False)
    assert drop_sheets.is_configured() is False


def test_drop_sheets_not_configured_when_creds_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GSHEETS_CREDENTIALS", str(tmp_path / "nonexistent.json"))
    monkeypatch.setenv("GSHEETS_SHEET_ID", "sheet-xyz")
    assert drop_sheets.is_configured() is False


def test_drop_sheets_not_configured_when_creds_empty(monkeypatch):
    monkeypatch.setenv("GSHEETS_CREDENTIALS", "")
    monkeypatch.setenv("GSHEETS_SHEET_ID", "sheet-xyz")
    assert drop_sheets.is_configured() is False


def test_drop_sheets_append_week_empty_rows_returns_ok():
    result = drop_sheets.append_week([])
    assert result == {"ok": True, "written": 0}


def test_drop_sheets_append_week_not_configured(monkeypatch):
    monkeypatch.delenv("GSHEETS_SHEET_ID", raising=False)
    result = drop_sheets.append_week([{"week": "2026-W23"}])
    assert result["ok"] is False
    assert result["reason"] == "not configured"


def test_drop_sheets_append_week_no_gspread(monkeypatch, tmp_path):
    """When gspread is not installed, append_week must return a clean error."""
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GSHEETS_CREDENTIALS", str(creds))
    monkeypatch.setenv("GSHEETS_SHEET_ID", "sheet-xyz")

    # Simulate gspread not being installed.
    def boom(*a, **kw):
        raise ModuleNotFoundError("No module named 'gspread'")

    monkeypatch.setattr(drop_sheets, "_open_worksheet", boom)
    result = drop_sheets.append_week([{"week": "2026-W23"}])
    assert result["ok"] is False
    assert "gspread" in result["reason"]
