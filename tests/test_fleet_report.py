"""Tests for watcherdog.fleet_report — deterministic report commands (Phase 2)."""

from __future__ import annotations

import asyncio
import json
import os
import types

from watcherdog import drop_stats, fleet_report
from watcherdog.farm_stats import BotStats


def _write_buffer(d, week, rows, generated="2026-06-10T00:00:05"):
    os.makedirs(d, exist_ok=True)
    path = drop_stats.buffer_path(d, week)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"week": week, "generated": generated, "rows": rows}, fh)
    return path


def test_load_latest_buffer_keys_rows_by_bot_number(tmp_path):
    _write_buffer(str(tmp_path), "2026-W24", [
        {"panel": "Panel#3", "drops": 28, "value": 31.5, "items": 2},
        {"panel": "Panel#7", "drops": 10, "value": 4.0, "items": 0},
    ])
    by_num, week, collected = fleet_report._load_latest_buffer(str(tmp_path))
    assert week == "2026-W24"
    assert collected == "2026-06-10"
    assert by_num[3]["value"] == 31.5
    assert by_num[7]["drops"] == 10


def test_load_latest_buffer_missing_dir_is_empty():
    by_num, week, collected = fleet_report._load_latest_buffer("/no/such/dir")
    assert by_num == {} and week is None and collected is None


def test_load_latest_buffer_falls_back_to_newest_week(tmp_path):
    _write_buffer(str(tmp_path), "2026-W22", [{"panel": "Panel#1", "drops": 1, "value": 1.0}])
    _write_buffer(str(tmp_path), "2026-W24", [{"panel": "Panel#1", "drops": 9, "value": 9.0}])
    by_num, week, _ = fleet_report._load_latest_buffer(str(tmp_path))
    assert week == "2026-W24" and by_num[1]["drops"] == 9


def _cfg():
    return types.SimpleNamespace(quiet_threshold_minutes=60, silence_threshold=1800,
                                 drop_stats_dir=None)


class _FakeClient:
    pass


def test_snapshot_merges_roster_sweep_with_buffer(tmp_path, monkeypatch):
    _write_buffer(str(tmp_path), "2026-W24", [
        {"panel": "Panel#3", "drops": 28, "value": 31.5, "items": 2},
    ])
    cfg = _cfg()
    cfg.drop_stats_dir = str(tmp_path)

    async def fake_latest(client, ent, mark_read=False):
        from datetime import datetime as _dt
        return ("[SinFermera3] warmup started", _dt.now())

    monkeypatch.setattr(fleet_report.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(fleet_report.roster, "load_pc_map", lambda cfg: {3: "PC1"})

    fleet = asyncio.run(fleet_report.snapshot(_FakeClient(), cfg, [("SinFermera3", object())]))
    assert fleet.week == "2026-W24"
    e = fleet.entries[0]
    assert e.num == 3 and e.pc == "PC1"
    assert e.stats.drops == 28 and e.stats.value_usd == 31.5
    assert e.stats.last_status == "warmup"
    assert e.stats.data_source == "text"


def test_snapshot_bot_without_buffer_row_has_none_drops(tmp_path, monkeypatch):
    cfg = _cfg()
    cfg.drop_stats_dir = str(tmp_path)   # empty dir -> no buffer

    async def fake_latest(client, ent, mark_read=False):
        from datetime import datetime as _dt
        return ("[SinFermera9] match ended", _dt.now())

    monkeypatch.setattr(fleet_report.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(fleet_report.roster, "load_pc_map", lambda cfg: {})

    fleet = asyncio.run(fleet_report.snapshot(_FakeClient(), cfg, [("SinFermera9", object())]))
    e = fleet.entries[0]
    assert e.num == 9 and e.stats.drops is None and e.stats.value_usd is None


def test_snapshot_skips_unnumbered_and_survives_read_error(monkeypatch):
    cfg = _cfg()

    async def boom(client, ent, mark_read=False):
        raise RuntimeError("read failed")

    monkeypatch.setattr(fleet_report.tg_tools, "latest_message", boom)
    monkeypatch.setattr(fleet_report.roster, "load_pc_map", lambda cfg: {})

    fleet = asyncio.run(fleet_report.snapshot(
        _FakeClient(), cfg, [("control bot", object()), ("SinFermera5", object())]))
    assert [e.num for e in fleet.entries] == [5]   # unnumbered skipped, error survived


def _entry(num, drops=None, value=None, status="✅ farming", age=5.0, text=""):
    st = BotStats(bot=f"SinFermera{num}", drops=drops, value_usd=value,
                  data_source=("text" if (drops is not None or value is not None) else "missing"))
    return fleet_report.FleetEntry(num=num, name=f"SinFermera{num}", pc="PC1",
                                   status=status, age_min=age, last_text=text, stats=st)


def _fleet(entries):
    return fleet_report.Fleet(entries=entries, week="2026-W24", collected="2026-06-10")


def test_weekly_totals_and_staleness_footer():
    fl = _fleet([_entry(3, 28, 31.5), _entry(7, 10, 4.0), _entry(9, 2, 0.6)])
    out = fleet_report.weekly(fl)
    assert "2026-W24" in out
    assert "40 cases" in out            # 28+10+2
    assert "$36.10" in out              # 31.5+4.0+0.6
    assert "2026-06-10" in out          # collection date footer


def test_weekly_no_collection_message():
    fl = _fleet([_entry(3), _entry(7)])   # no drop data anywhere
    out = fleet_report.weekly(fl)
    assert "no drop collection yet" in out.lower()
    assert "drop stats" in out.lower()


def test_value_grand_total_and_top_contributors():
    fl = _fleet([_entry(3, 28, 31.5), _entry(7, 10, 4.0)])
    out = fleet_report.value(fl)
    assert "$35.50" in out
    assert out.index("SF3") < out.index("SF7")   # highest value first


def test_top_orders_by_value_desc():
    fl = _fleet([_entry(7, 10, 4.0), _entry(3, 28, 31.5), _entry(9, 2, 0.6)])
    out = fleet_report.top(fl, n=2)
    assert "SF3" in out and "SF7" in out and "SF9" not in out   # top 2 only
    assert out.index("SF3") < out.index("SF7")


def test_worst_flags_silent_and_orders_by_value_asc():
    fl = _fleet([_entry(3, 28, 31.5),
                 _entry(7, 0, 0.0, status="💀 dead", age=400.0)])
    out = fleet_report.worst(fl, n=2)
    assert out.index("SF7") < out.index("SF3")   # lowest value first
    assert "💀" in out


def test_weekly_top_bottom_no_overlap_with_five_bots():
    fl = _fleet([_entry(1, 50, 50.0), _entry(2, 40, 40.0), _entry(3, 30, 30.0),
                 _entry(4, 20, 20.0), _entry(5, 10, 10.0)])
    out = fleet_report.weekly(fl)
    # SF3 is the middle bot — must NOT appear in both Top and Bottom (i.e. once total)
    assert out.count("SF3") == 1
    assert "SF1" in out and "SF5" in out      # top and bottom extremes present


def test_check_one_bot_reports_status_and_drops():
    fl = _fleet([_entry(3, 28, 31.5, text="[SinFermera3] warmup started")])
    out = fleet_report.check(fl, 3)
    assert "SF3" in out and "28 cases" in out and "$31.50" in out


def test_check_unknown_bot():
    fl = _fleet([_entry(3, 28, 31.5)])
    assert "SF8" in fleet_report.check(fl, 8) and "not" in fleet_report.check(fl, 8).lower()


def test_compare_two_bots_side_by_side():
    fl = _fleet([_entry(3, 28, 31.5), _entry(7, 10, 4.0)])
    out = fleet_report.compare(fl, 3, 7)
    assert "SF3" in out and "SF7" in out
    assert out.index("SF3") < out.index("SF7")


def test_bans_lists_critical_bots_only():
    fl = _fleet([
        _entry(3, 28, 31.5, text="[SinFermera3] warmup started"),
        _entry(7, 0, 0.0, status="🔴 needs attention",
               text="[SinFermera7] account banned by VAC"),
    ])
    out = fleet_report.bans(fl)
    assert "SF7" in out and "SF3" not in out


def test_bans_none_clean_message():
    fl = _fleet([_entry(3, 28, 31.5, text="[SinFermera3] warmup started")])
    assert "no bans" in fleet_report.bans(fl).lower()


def test_today_shows_live_status_and_week_drops():
    fl = _fleet([_entry(3, 28, 31.5, status="✅ farming"),
                 _entry(7, 10, 4.0, status="💀 dead", age=400.0)])
    out = fleet_report.today(fl)
    assert "1/2 farming" in out          # one farming of two
    assert "38 cases" in out             # 28+10 this week so far
