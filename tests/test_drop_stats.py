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


def test_drop_sheets_append_week_append_rows_failure(monkeypatch, tmp_path):
    """append_rows raising returns ok=False with the error reason."""
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GSHEETS_CREDENTIALS", str(creds))
    monkeypatch.setenv("GSHEETS_SHEET_ID", "sheet-xyz")

    fake_ws = type("WS", (), {"append_rows": lambda *a, **kw: (_ for _ in ()).throw(IOError("quota"))})()

    monkeypatch.setattr(drop_sheets, "_open_worksheet", lambda: fake_ws)
    result = drop_sheets.append_week([{"week": "2026-W23", "panel": "P1"}])
    assert result["ok"] is False
    assert "quota" in result["reason"]


def test_drop_sheets_append_week_success(monkeypatch, tmp_path):
    """append_week returns ok=True with written count when gspread succeeds."""
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("GSHEETS_CREDENTIALS", str(creds))
    monkeypatch.setenv("GSHEETS_SHEET_ID", "sheet-xyz")

    appended = []
    fake_ws = type("WS", (), {"append_rows": lambda self, rows, **kw: appended.extend(rows)})()
    monkeypatch.setattr(drop_sheets, "_open_worksheet", lambda: fake_ws)

    rows = [{"week": "2026-W23", "panel": "Panel#1", "drops": "300"}]
    result = drop_sheets.append_week(rows)
    assert result["ok"] is True
    assert result["written"] == 1
    assert len(appended) == 1


# --- _folder_ref ------------------------------------------------------------

def test_folder_ref_numeric_string_returns_int():
    assert drop_stats._folder_ref("42") == 42


def test_folder_ref_negative_numeric_string():
    assert drop_stats._folder_ref("-100123") == -100123


def test_folder_ref_name_returns_string():
    assert drop_stats._folder_ref("Farms") == "Farms"


def test_folder_ref_already_int():
    assert drop_stats._folder_ref(7) == 7


# --- _await_reply (async) ---------------------------------------------------

def test_await_reply_returns_first_incoming_message():
    import asyncio
    from types import SimpleNamespace

    msg = SimpleNamespace(out=False, id=10, buttons=None, message="hello")

    class _Client:
        async def get_messages(self, ent, limit=6):
            return [msg]

    result = asyncio.run(drop_stats._await_reply(_Client(), "ent", after_id=5, timeout=1.0))
    assert result is msg


def test_await_reply_skips_own_messages():
    import asyncio
    from types import SimpleNamespace

    own = SimpleNamespace(out=True, id=10, buttons=None, message="my own")
    incoming = SimpleNamespace(out=False, id=11, buttons=None, message="reply")

    class _Client:
        async def get_messages(self, ent, limit=6):
            return [incoming, own]

    result = asyncio.run(drop_stats._await_reply(_Client(), "ent", after_id=5, timeout=1.0))
    assert result is incoming


def test_await_reply_times_out_when_no_message():
    import asyncio
    from types import SimpleNamespace

    class _Client:
        async def get_messages(self, ent, limit=6):
            return []

    result = asyncio.run(drop_stats._await_reply(_Client(), "ent", after_id=None,
                                                  timeout=0.05, poll=0.01))
    assert result is None


def test_await_reply_skips_stale_messages():
    import asyncio
    from types import SimpleNamespace

    stale = SimpleNamespace(out=False, id=3, buttons=None, message="old")

    class _Client:
        async def get_messages(self, ent, limit=6):
            return [stale]

    result = asyncio.run(drop_stats._await_reply(_Client(), "ent", after_id=5,
                                                  timeout=0.05, poll=0.01))
    assert result is None


def test_await_reply_need_buttons_skips_buttonless():
    import asyncio
    from types import SimpleNamespace

    no_buttons = SimpleNamespace(out=False, id=10, buttons=None, message="no buttons")
    with_buttons = SimpleNamespace(out=False, id=11, buttons=[[]], message="has buttons")

    class _Client:
        async def get_messages(self, ent, limit=6):
            return [with_buttons, no_buttons]

    result = asyncio.run(drop_stats._await_reply(_Client(), "ent", after_id=5,
                                                  need_buttons=True, timeout=1.0))
    assert result is with_buttons


def test_await_reply_client_exception_returns_none():
    import asyncio

    class _BrokenClient:
        async def get_messages(self, ent, limit=6):
            raise OSError("disconnected")

    result = asyncio.run(drop_stats._await_reply(_BrokenClient(), "ent",
                                                  after_id=None, timeout=0.05, poll=0.01))
    assert result is None


# --- _press (async) ---------------------------------------------------------

def test_press_clicks_matching_button():
    import asyncio
    from types import SimpleNamespace

    clicked = []

    class _Btn:
        text = "Kill All CS"
        async def click(self): clicked.append(self.text)

    class _Msg:
        buttons = [[_Btn()]]
        async def click(self, text=None):
            clicked.append(text)

    msg = _Msg()
    result = asyncio.run(drop_stats._press(msg, ("kill all",)))
    assert result is True
    assert clicked


def test_press_no_matching_button_returns_false():
    import asyncio
    from types import SimpleNamespace

    class _Btn:
        text = "Some Other Button"

    class _Msg:
        buttons = [[_Btn()]]

    result = asyncio.run(drop_stats._press(_Msg(), ("kill all",)))
    assert result is False


def test_press_no_buttons_returns_false():
    import asyncio
    from types import SimpleNamespace

    class _Msg:
        buttons = None

    result = asyncio.run(drop_stats._press(_Msg(), ("any",)))
    assert result is False


# --- stop_farm / request_drop_stats / run_activity_booster ------------------

def test_stop_farm_returns_false_when_no_menu(monkeypatch):
    import asyncio

    async def fake_open_menu(client, ent, **kw):
        return None  # simulates no /start reply

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    result = asyncio.run(drop_stats.stop_farm(None, "ent"))
    assert result is False


def test_stop_farm_returns_false_when_no_stop_button(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]])  # menu exists but no stop button

    async def fake_press(msg, prefixes):
        return False

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    result = asyncio.run(drop_stats.stop_farm(None, "ent"))
    assert result is False


def test_request_drop_stats_returns_empty_when_no_menu(monkeypatch):
    import asyncio

    async def fake_open_menu(client, ent, **kw):
        return None

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    result = asyncio.run(drop_stats.request_drop_stats(None, "ent"))
    assert result == ""


def test_request_drop_stats_returns_empty_when_no_drops_button(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]], id=1)

    async def fake_press(msg, prefixes):
        return False

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    result = asyncio.run(drop_stats.request_drop_stats(None, "ent"))
    assert result == ""


def test_request_drop_stats_returns_reply_text(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]], id=5)

    async def fake_press(msg, prefixes):
        return True

    async def fake_await_reply(client, ent, after_id, **kw):
        return SimpleNamespace(message="312 drops · $45")

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    monkeypatch.setattr(drop_stats, "_await_reply", fake_await_reply)
    result = asyncio.run(drop_stats.request_drop_stats(None, "ent"))
    assert "312" in result


def test_run_activity_booster_returns_false_when_no_menu(monkeypatch):
    import asyncio

    async def fake_open_menu(client, ent, **kw):
        return None

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    result = asyncio.run(drop_stats.run_activity_booster(None, "ent"))
    assert result is False


# --- _send (multi-target) in drop_stats context ----------------------------

def test_send_to_list_delivers_all():
    import asyncio

    sent = []

    class _Client:
        async def send_message(self, t, text, **kw):
            sent.append(t)

    result = asyncio.run(drop_stats._send(_Client(), ["a", "b"], "msg", deliver=True))
    assert result is True
    assert sent == ["a", "b"]


def test_send_to_empty_list_returns_false():
    import asyncio

    class _Client:
        async def send_message(self, *a, **kw): pass

    result = asyncio.run(drop_stats._send(_Client(), [], "msg", deliver=True))
    assert result is False


def test_send_dry_run_does_not_call_send():
    import asyncio

    sent = []

    class _Client:
        async def send_message(self, *a, **kw):
            sent.append(True)

    result = asyncio.run(drop_stats._send(_Client(), "ibo", "msg", deliver=False))
    assert result is True
    assert sent == []


# --- run_weekly (integration with mocks) ------------------------------------

def test_run_weekly_returns_structure(monkeypatch, tmp_path):
    import asyncio

    async def fake_load_panels(client, cfg):
        return [("Panel 1", "ent1")]

    async def fake_collect(client, cfg, panels, *, week, date=None, deliver=True):
        return [drop_stats.make_row(week, "Panel#1", {"drops": "10"}, date=date)]

    monkeypatch.setattr(drop_stats, "load_panels", fake_load_panels)
    monkeypatch.setattr(drop_stats, "collect_week", fake_collect)
    monkeypatch.setattr(drop_stats, "push_to_sheets", lambda cfg, rows: {"ok": False, "reason": "not configured"})

    cfg = Config({"DROP_STATS_DIR": str(tmp_path / "drop_stats")})
    result = asyncio.run(drop_stats.run_weekly(None, cfg, target=None, deliver=False,
                                                now=datetime(2026, 6, 3, 0, 0)))
    assert result["week"] == "2026-W23"
    assert len(result["rows"]) == 1
    assert "path" in result
