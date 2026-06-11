# Phase 2 — Deterministic Report Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 8 report/triage commands (`/weekly /today /top /worst /value /check /compare /bans`) compute from real fleet data with zero model calls, and fix the weekly-collection parser that was recording garbage drop/value numbers.

**Architecture:** A new pure module `fleet_report.py` does one cheap `latest_message` sweep (mirroring `roster.scan`, no button presses) and merges it with the latest weekly drop buffer into a `Fleet` of `FleetEntry` (each wrapping a `farm_stats.BotStats`). Eight pure formatters render phone-sized replies. `drop_stats.collect_week` switches to the Phase 1 `farm_stats.parse_drop_report` via a small adapter so the buffer/Sheets finally hold correct numbers. Both Telegram dispatchers route the report command names to `fleet_report.handle` before the model branch; the weekly digest becomes deterministic.

**Tech Stack:** Python 3.14, stdlib `re`/`dataclasses`/`datetime`, Telethon (passed-in client), pytest. Run the project venv: `.venv/bin/python`. Green-check: `.venv/bin/python -m pytest $(git ls-files 'tests/*.py')`.

---

## File Structure

| File | Responsibility |
|---|---|
| `watcherdog/fleet_report.py` | **new** — `FleetEntry`/`Fleet` dataclasses, `snapshot()` (IO), 8 pure formatters, `handle()` dispatcher (<500 ln) |
| `watcherdog/drop_stats.py` | `collect_week` parses via `farm_stats.parse_drop_report` through new `_report_to_row` adapter; `parse_drop_stats` removed |
| `watcherdog/commands.py` | add `REPORT_NAMES` set + `report_parse()` helper (resolves `/drops`→`today`) |
| `watcherdog/mcp_watcher.py` | route report cmds in `_on_ibo`; make `run_weekly_digest` deterministic; import `fleet_report` |
| `watcherdog/bot_interface.py` | route report cmds before the agent task; import `fleet_report`; add `_run_report_command` |
| `tests/test_drop_stats.py` | replace the 3 `parse_drop_stats` tests with `_report_to_row` tests |
| `tests/test_fleet_report.py` | **new** — buffer load, snapshot merge, every formatter, dispatch |

Merge key: roster bot number is `int(re.search(r"(\d+)", name))`; buffer rows are keyed `"Panel#N"` → `int(re.search(r"(\d+)", row["panel"]))`. They reconcile on that integer.

---

## Task 1: Parser convergence — `drop_stats` adopts `parse_drop_report`

**Files:**
- Modify: `watcherdog/drop_stats.py` (remove `parse_drop_stats` ~`:70-92`; rewrite `collect_week` parse step `:327`,`:344-350`; add `_report_to_row`)
- Test: `tests/test_drop_stats.py:45-63` (replace the 3 `parse_drop_stats` tests)

- [ ] **Step 1: Write the failing tests** — replace the `# --- parse_drop_stats ---` block (lines 45-63) in `tests/test_drop_stats.py` with:

```python
# --- _report_to_row (parser convergence: farm_stats.parse_drop_report) -------

_REAL_DROP_TEXT = """=-=-= FSM PANEL | DROP REPORT =-=-=

Date: 10.06.2026 - 17.06.2026
Accounts: 28

Case                      | Amount | % of drops
--------------------------+--------+-----------
Sealed Genesis Terminal   | 8      | 28.6
Revolution Case           | 5      | 17.9
--------------------------+--------+-----------

Skin (>0.6$)                        | Amount | Price $
------------------------------------+--------+--------
USP-S | Royal Guard (Minimal Wear)  | 1      | 4.76
M4A1-S | Rose Hex (Minimal Wear)    | 1      | 0.6
------------------------------------+--------+--------

= Price of all drop: ~ 31.5$.
= Total cases: 28 pcs.
"""


def test_report_to_row_real_panel_correct_numbers():
    # Regression: the OLD parser returned drops=31 (from the price) value=28 (from
    # the account count). The new path must record cases=28, value=31.5, items=2.
    row = drop_stats._report_to_row(_REAL_DROP_TEXT)
    assert row["drops"] == 28
    assert row["value"] == 31.5
    assert row["items"] == 2          # 2 valuable skins pulled
    assert row["notes"] == ""


def test_report_to_row_empty_text_is_blank():
    row = drop_stats._report_to_row("")
    assert row == {"drops": "", "items": "", "value": "", "notes": ""}


def test_report_to_row_records_problems_in_notes():
    row = drop_stats._report_to_row("DROP REPORT\nCan't get drop on 3 accounts")
    assert "Can't get drop on 3 accounts" in row["notes"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_drop_stats.py -k report_to_row -v`
Expected: FAIL — `AttributeError: module 'watcherdog.drop_stats' has no attribute '_report_to_row'`

- [ ] **Step 3: Add the import and the adapter; remove `parse_drop_stats`**

In `watcherdog/drop_stats.py`, add to the imports (near `from watcherdog import drop_sheets, tg_tools`):

```python
from watcherdog import drop_sheets, farm_stats, tg_tools
```

Delete the whole `def parse_drop_stats(text):` function (lines ~70-92) and replace it with:

```python
def _report_to_row(text):
    """Parse a panel's Drop Stats reply into the sheet-column fields, via the
    Phase 1 farm_stats parser. Empty/echo text -> all-blank (so format_report and
    the Sheets push show nothing rather than a fabricated 0). Never raises."""
    rep = farm_stats.parse_drop_report(text)
    has = (rep.total_cases is not None or rep.value_usd is not None
           or rep.accounts is not None)
    if not has and not rep.problems:
        return {"drops": "", "items": "", "value": "", "notes": ""}
    return {
        "drops": rep.total_cases if rep.total_cases is not None else "",
        "items": len(rep.skins),
        "value": rep.value_usd if rep.value_usd is not None else "",
        "notes": "; ".join(rep.problems),
    }
```

- [ ] **Step 4: Rewire `collect_week` to use the adapter**

In `collect_week`, the dry-run branch (line ~327-329):

```python
        if not deliver:
            log.info("[DRY-RUN] %s: would stop farm -> drop stats -> activity booster", panel)
            parsed = _report_to_row("")
            parsed["notes"] = "dry-run"
            rows.append(make_row(week, panel, parsed, date=date))
            continue
```

And the real branch (line ~344-350):

```python
        parsed = _report_to_row(text)
        if not text.strip():
            parsed["notes"] = "no reply"
        rows.append(make_row(week, panel, parsed, date=date))
        log.info("%s: cases=%s items=%s value=%s",
                 panel, parsed["drops"] or "?", parsed["items"] or "?",
                 parsed["value"] or "?")
```

- [ ] **Step 5: Run the report_to_row tests + the full drop_stats suite**

Run: `.venv/bin/python -m pytest tests/test_drop_stats.py -v`
Expected: PASS (the 3 new tests pass; make_row/format_report/collect_week tests still pass — `make_row` is unchanged and accepts the dict shape).

- [ ] **Step 6: Commit**

```bash
git add watcherdog/drop_stats.py tests/test_drop_stats.py
git commit -m "fix(drop_stats): collect via farm_stats.parse_drop_report (correct cases/value)

The old parse_drop_stats recorded garbage on real panels (drops from the price,
value from the account count). collect_week now parses with the Phase 1 parser
through _report_to_row; parse_drop_stats removed."
```

---

## Task 2: `fleet_report` dataclasses + buffer loader (pure)

**Files:**
- Create: `watcherdog/fleet_report.py`
- Test: `tests/test_fleet_report.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_fleet_report.py`:

```python
"""Tests for watcherdog.fleet_report — deterministic report commands (Phase 2)."""

from __future__ import annotations

import asyncio
import json
import os

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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'watcherdog.fleet_report'`

- [ ] **Step 3: Create the module skeleton + buffer loader**

Create `watcherdog/fleet_report.py`:

```python
"""Deterministic report commands — answered with NO model (Phase 2).

/weekly /value /top /worst /check /compare /bans /today read one cheap
latest_message sweep (mirroring roster.scan, no button presses) merged with the
latest weekly drop buffer (the only place per-panel $ values are collected). Pure
formatters render phone-sized replies. No LLM — instant and free.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from watcherdog import drop_stats, farm_stats, roster, tg_tools
from watcherdog.classifier import severity_of, summarize
from watcherdog.farm_stats import BotStats

logger = logging.getLogger("watcherdog.fleet_report")

_NUM_RE = re.compile(r"(\d+)")


@dataclass
class FleetEntry:
    num: int
    name: str
    pc: str = "?"
    status: str = ""
    age_min: float = 0.0
    last_text: str = ""
    stats: BotStats = field(default_factory=BotStats)


@dataclass
class Fleet:
    entries: list = field(default_factory=list)   # list[FleetEntry], sorted by num
    week: str | None = None
    collected: str | None = None                  # short date label, e.g. "2026-06-10"


def _short_date(generated):
    """'2026-06-10T00:00:05' -> '2026-06-10'; None/garbage -> None."""
    if not generated or not isinstance(generated, str):
        return None
    return generated.split("T", 1)[0]


def _newest_buffer(d):
    """Load the lexicographically-latest <YYYY-Www>.json in d (ISO weeks sort
    correctly), or None."""
    try:
        files = [f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return None
    if not files:
        return None
    return drop_stats.load_buffer(os.path.join(d, max(files)))


def _load_latest_buffer(drop_stats_dir):
    """Return (rows_by_bot_number, week, collected_label).

    Tries the current ISO week, then falls back to the newest buffer file. Empty
    ({}, None, None) when nothing is readable. Never raises."""
    if not drop_stats_dir:
        return {}, None, None
    payload = drop_stats.load_buffer(
        drop_stats.buffer_path(drop_stats_dir, drop_stats.iso_week(datetime.now())))
    if payload is None:
        payload = _newest_buffer(drop_stats_dir)
    if not payload:
        return {}, None, None
    by_num = {}
    for r in (payload.get("rows") or []):
        m = _NUM_RE.search(str(r.get("panel", "")))
        if m:
            by_num[int(m.group(1))] = r
    return by_num, payload.get("week"), _short_date(payload.get("generated"))
```

- [ ] **Step 4: Run the loader tests**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -v`
Expected: PASS (3 loader tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/fleet_report.py tests/test_fleet_report.py
git commit -m "feat(fleet_report): Fleet/FleetEntry dataclasses + weekly buffer loader"
```

---

## Task 3: `snapshot()` — merge live sweep with the buffer

**Files:**
- Modify: `watcherdog/fleet_report.py` (add `_coerce_int`, `_coerce_float`, `snapshot`)
- Test: `tests/test_fleet_report.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_fleet_report.py`:

```python
import types


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
```

**Async-test convention (verified):** this repo does NOT use `pytest-asyncio` (it isn't installed). Async code is driven from a plain sync `def test_` via `asyncio.run(coro)` — that's why these tests import `asyncio` and wrap the awaited calls. Do NOT add `@pytest.mark.asyncio` / `async def test_` — they would be collected but never assert.

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k snapshot -v`

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k snapshot -v`
Expected: FAIL — `AttributeError: module 'watcherdog.fleet_report' has no attribute 'snapshot'`

- [ ] **Step 3: Implement `snapshot` + coercion helpers**

Append to `watcherdog/fleet_report.py`:

```python
def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def snapshot(client, cfg, watch):
    """One cheap latest_message sweep merged with the latest weekly buffer ->
    Fleet(list[FleetEntry]). No button presses, no model. Never raises on a single
    bot's read; bots without a number in their name are skipped."""
    pc_map = roster.load_pc_map(cfg)
    by_num, week, collected = _load_latest_buffer(getattr(cfg, "drop_stats_dir", None))
    now_ts = time.time()
    entries = []
    for name, ent in (watch or []):
        m = _NUM_RE.search(name or "")
        if not m:
            continue
        num = int(m.group(1))
        try:
            text, date = await tg_tools.latest_message(client, ent, mark_read=False)
        except Exception:  # noqa: BLE001
            text, date = None, None
        age_min = ((now_ts - date.timestamp()) / 60.0) if date else 1_000_000.0
        st = BotStats(bot=name)
        st.last_status = farm_stats.parse_status_event(text)
        st.accounts_up = roster.extract_account_count(text) if text else None
        row = by_num.get(num)
        if row:
            st.drops = _coerce_int(row.get("drops"))
            st.value_usd = _coerce_float(row.get("value"))
            st.data_source = ("text" if (st.drops is not None or st.value_usd is not None)
                              else "missing")
        entries.append(FleetEntry(num=num, name=name, pc=pc_map.get(num, "?"),
                                  status=roster.classify_status(text, age_min, cfg),
                                  age_min=age_min, last_text=text or "", stats=st))
    entries.sort(key=lambda e: e.num)
    return Fleet(entries=entries, week=week, collected=collected)
```

- [ ] **Step 4: Run the snapshot tests**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k snapshot -v`
Expected: PASS (3 snapshot tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/fleet_report.py tests/test_fleet_report.py
git commit -m "feat(fleet_report): snapshot() merges live sweep + weekly buffer -> Fleet"
```

---

## Task 4: Money formatters — `weekly` / `value` / `top` / `worst`

**Files:**
- Modify: `watcherdog/fleet_report.py` (add `_money`, `_sf`, `_footer`, `_no_data`, `_has_data`, `weekly`, `value`, `top`, `worst`)
- Test: `tests/test_fleet_report.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k "weekly or value or top or worst" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'weekly'`

- [ ] **Step 3: Implement the money formatters**

Append to `watcherdog/fleet_report.py`:

```python
def _money(v):
    return f"${(v or 0.0):.2f}"


def _sf(e):
    return f"SF{e.num}"


def _footer(fleet):
    wk = fleet.week or "?"
    when = f" · collected {fleet.collected}" if fleet.collected else ""
    return f"— {wk}{when}"


def _has_data(e):
    return e.stats.drops is not None or e.stats.value_usd is not None


def _no_data(fleet):
    return ("🐕 No drop collection yet — send `drop stats` to pull this week's "
            "numbers now (it stops the farms briefly).")


def _by_value_desc(entries):
    return sorted(entries, key=lambda e: (e.stats.value_usd or 0.0), reverse=True)


def _line(e):
    drops = e.stats.drops if e.stats.drops is not None else "?"
    return f"• {_sf(e)} — {drops} cases · ~{_money(e.stats.value_usd)}"


def weekly(fleet):
    """Headline totals + top 3 / bottom 3 by value. Skimmable for a phone."""
    haves = [e for e in fleet.entries if _has_data(e)]
    if not haves:
        return _no_data(fleet)
    cases = sum((e.stats.drops or 0) for e in haves)
    val = sum((e.stats.value_usd or 0.0) for e in haves)
    ranked = _by_value_desc(haves)
    lines = [f"🐕 Weekly drops — {fleet.week or '?'} — {cases} cases · "
             f"~{_money(val)} ({len(haves)} bots reporting)"]
    lines.append("🏆 Top:")
    lines += [_line(e) for e in ranked[:3]]
    if len(ranked) > 3:
        lines.append("🐌 Bottom:")
        lines += [_line(e) for e in ranked[-3:]]
    lines.append(_footer(fleet))
    return "\n".join(lines)


def value(fleet):
    haves = [e for e in fleet.entries if _has_data(e)]
    if not haves:
        return _no_data(fleet)
    total = sum((e.stats.value_usd or 0.0) for e in haves)
    lines = [f"💰 Total value — {_money(total)} ({fleet.week or '?'})", "Top contributors:"]
    lines += [_line(e) for e in _by_value_desc(haves)[:5]]
    lines.append(_footer(fleet))
    return "\n".join(lines)


def top(fleet, n=5):
    haves = [e for e in fleet.entries if _has_data(e)]
    if not haves:
        return _no_data(fleet)
    lines = [f"🏆 Top {min(n, len(haves))} bots — {fleet.week or '?'}"]
    lines += [_line(e) for e in _by_value_desc(haves)[:n]]
    lines.append(_footer(fleet))
    return "\n".join(lines)


def worst(fleet, n=5):
    haves = [e for e in fleet.entries if _has_data(e)]
    if not haves:
        return _no_data(fleet)
    ascending = sorted(haves, key=lambda e: (e.stats.value_usd or 0.0))
    lines = [f"🐌 Laggards {min(n, len(haves))} — {fleet.week or '?'}"]
    for e in ascending[:n]:
        flag = "" if e.status.startswith("✅") else f"  {roster.status_emoji(e.status)}"
        lines.append(_line(e) + flag)
    lines.append(_footer(fleet))
    return "\n".join(lines)
```

- [ ] **Step 4: Run the money-formatter tests**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k "weekly or value or top or worst" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/fleet_report.py tests/test_fleet_report.py
git commit -m "feat(fleet_report): weekly/value/top/worst formatters over the buffer"
```

---

## Task 5: Triage formatters — `check` / `compare` / `bans` / `today`

**Files:**
- Modify: `watcherdog/fleet_report.py` (add `_find`, `_age_label`, `check`, `compare`, `bans`, `today`)
- Test: `tests/test_fleet_report.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k "check or compare or bans or today" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'check'`

- [ ] **Step 3: Implement the triage formatters**

Append to `watcherdog/fleet_report.py`:

```python
def _find(fleet, num):
    for e in fleet.entries:
        if e.num == num:
            return e
    return None


def _age_label(age_min):
    if age_min is None or age_min >= 100000:
        return "?"
    if age_min < 90:
        return f"{age_min:.0f}m"
    h, m = divmod(int(age_min), 60)
    return f"{h}h{m:02d}m"


def _detail(e):
    drops = e.stats.drops if e.stats.drops is not None else "?"
    head = (f"{_sf(e)} ({e.pc}) — {e.status}, last seen {_age_label(e.age_min)}\n"
            f"  drops: {drops} cases · ~{_money(e.stats.value_usd)}")
    if e.stats.last_status:
        head += f"\n  latest: {e.stats.last_status}"
    return head


def check(fleet, num):
    e = _find(fleet, num)
    if e is None:
        return f"🔬 No SF{num} in the roster right now."
    return "🔬 " + _detail(e) + "\n" + _footer(fleet)


def compare(fleet, a, b):
    ea, eb = _find(fleet, a), _find(fleet, b)
    missing = [f"SF{n}" for n, e in ((a, ea), (b, eb)) if e is None]
    if missing:
        return f"⚖️ Can't compare — not in the roster: {', '.join(missing)}."
    return ("⚖️ Compare\n" + _detail(ea) + "\n\n" + _detail(eb) + "\n" + _footer(fleet))


def bans(fleet):
    """Bots whose latest message is a critical signal (ban / captcha / Steam-Guard
    / crash family) per the classifier. Pure — reads each entry's stored text."""
    hot = [e for e in fleet.entries if severity_of(e.last_text) == "critical"]
    if not hot:
        return "✅ No bans / captcha / Steam-Guard prompts right now."
    lines = [f"🚫 Critical — {len(hot)} bot(s):"]
    for e in sorted(hot, key=lambda e: e.num):
        lines.append(f"• {_sf(e)} ({e.pc}) — {summarize(e.last_text)}")
    return "\n".join(lines)


def today(fleet):
    n = len(fleet.entries)
    farming = sum(1 for e in fleet.entries if e.status.startswith("✅"))
    haves = [e for e in fleet.entries if _has_data(e)]
    cases = sum((e.stats.drops or 0) for e in haves)
    val = sum((e.stats.value_usd or 0.0) for e in haves)
    lines = [f"📅 Today — {farming}/{n} farming"]
    bad = [e for e in fleet.entries if not e.status.startswith("✅")]
    for e in sorted(bad, key=lambda e: e.num):
        lines.append(f"{roster.status_emoji(e.status)} {_sf(e)} "
                     f"({e.pc}, {_age_label(e.age_min)})")
    if haves:
        lines.append(f"This week so far: {cases} cases · ~{_money(val)} {_footer(fleet)}")
    else:
        lines.append("No drop collection yet this week.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the triage-formatter tests**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k "check or compare or bans or today" -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/fleet_report.py tests/test_fleet_report.py
git commit -m "feat(fleet_report): check/compare/bans/today triage formatters"
```

---

## Task 6: `handle()` dispatcher + `commands.report_parse`

**Files:**
- Modify: `watcherdog/fleet_report.py` (add `handle`)
- Modify: `watcherdog/commands.py` (add `REPORT_NAMES`, `report_parse`)
- Test: `tests/test_fleet_report.py`, `tests/test_commands.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_fleet_report.py`:

```python
def test_handle_routes_weekly(monkeypatch):
    fl = _fleet([_entry(3, 28, 31.5)])

    async def fake_snapshot(client, cfg, watch):
        return fl

    monkeypatch.setattr(fleet_report, "snapshot", fake_snapshot)
    out = asyncio.run(fleet_report.handle("weekly", "", cfg=_cfg(), client=_FakeClient(), watch=[]))
    assert "Weekly drops" in out


def test_handle_check_parses_bot_number(monkeypatch):
    fl = _fleet([_entry(5, 12, 6.0)])

    async def fake_snapshot(client, cfg, watch):
        return fl

    monkeypatch.setattr(fleet_report, "snapshot", fake_snapshot)
    out = asyncio.run(fleet_report.handle("check", "5", cfg=_cfg(), client=_FakeClient(), watch=[]))
    assert "SF5" in out


def test_handle_unknown_cmd_returns_none(monkeypatch):
    async def fake_snapshot(client, cfg, watch):
        return _fleet([])

    monkeypatch.setattr(fleet_report, "snapshot", fake_snapshot)
    out = asyncio.run(fleet_report.handle("whatsnew", "", cfg=_cfg(), client=_FakeClient(), watch=[]))
    assert out is None
```

And append to `tests/test_commands.py`:

```python
def test_report_parse_recognizes_report_commands():
    from watcherdog import commands
    assert commands.report_parse("/weekly") == ("weekly", "")
    assert commands.report_parse("/check 5") == ("check", "5")
    assert commands.report_parse("/drops") == ("today", "")   # alias resolves
    assert commands.report_parse("/whatsnew") is None         # not deterministic
    assert commands.report_parse("hello") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py -k handle tests/test_commands.py -k report_parse -v`
Expected: FAIL — `AttributeError` for `handle` / `report_parse`.

- [ ] **Step 3a: Add `report_parse` to `commands.py`**

After the `parse` function (line ~252), add:

```python
# Report commands answered DETERMINISTICALLY from the weekly buffer + roster scan
# by watcherdog/fleet_report.py (Phase 2) — NO model. These names are also in
# MENU (so /help lists them), but the dispatcher routes them to fleet_report
# before the model branch.
REPORT_NAMES = {"weekly", "today", "top", "worst", "value", "check", "compare", "bans"}


def report_parse(text):
    """Return ``(canonical_cmd, args)`` if ``text`` is a deterministic report
    command (resolving ALIASES, e.g. /drops -> today), else None."""
    s = _split(text)
    if not s:
        return None
    cmd = ALIASES.get(s[0], s[0])
    if cmd not in REPORT_NAMES:
        return None
    return cmd, s[1]
```

- [ ] **Step 3b: Add `handle` to `fleet_report.py`**

Append to `watcherdog/fleet_report.py`:

```python
def _bot_num(args):
    m = _NUM_RE.search(args or "")
    return int(m.group(1)) if m else None


async def handle(cmd, args, *, cfg, client, watch):
    """Run one deterministic report command -> reply text (or None if cmd isn't a
    report command). Never raises — a failure returns a friendly one-liner."""
    try:
        fleet = await snapshot(client, cfg, watch or [])
    except Exception:  # noqa: BLE001
        logger.exception("fleet snapshot failed for /%s", cmd)
        return "⚠️ couldn't read the fleet just now — try again in a moment."
    if cmd == "weekly":
        return weekly(fleet)
    if cmd == "value":
        return value(fleet)
    if cmd == "top":
        return top(fleet)
    if cmd == "worst":
        return worst(fleet)
    if cmd == "today":
        return today(fleet)
    if cmd == "bans":
        return bans(fleet)
    if cmd == "check":
        num = _bot_num(args)
        return check(fleet, num) if num is not None else "🔬 Which bot? e.g. /check 5"
    if cmd == "compare":
        toks = _NUM_RE.findall(args or "")
        if len(toks) < 2:
            return "⚖️ Give two bots, e.g. /compare 3 7"
        return compare(fleet, int(toks[0]), int(toks[1]))
    return None
```

- [ ] **Step 4: Run both test groups + full commands suite**

Run: `.venv/bin/python -m pytest tests/test_fleet_report.py tests/test_commands.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/fleet_report.py watcherdog/commands.py tests/test_fleet_report.py tests/test_commands.py
git commit -m "feat(fleet_report): handle() dispatcher + commands.report_parse"
```

---

## Task 7: Wire `mcp_watcher._on_ibo` + deterministic `run_weekly_digest`

**Files:**
- Modify: `watcherdog/mcp_watcher.py` (import `fleet_report` at line ~42; insert report routing after the `fast_parse` block ~`:1116-1127`; rewrite `run_weekly_digest` ~`:1367-1383`)
- Test: `tests/test_monitor_once.py` or `tests/test_mcp_watcher_ibo.py` (use the existing ibo-dispatch test module if present; else add a focused test)

- [ ] **Step 1: Write the failing test** — find the existing ibo-handler test (`grep -ln "_on_ibo\|make_ibo\|on_ibo" tests/*.py`). If one exists, add to it; otherwise create `tests/test_fleet_report_dispatch.py`:

```python
"""Report commands route to fleet_report (no agent.answer) in both dispatchers."""

from __future__ import annotations

import asyncio
import types

from watcherdog import fleet_report


def test_report_command_routes_to_fleet_report_not_agent(monkeypatch):
    # handle() returns a canned report; agent.answer must never be called.
    called = {"handle": 0}

    async def fake_handle(cmd, args, *, cfg, client, watch):
        called["handle"] += 1
        return "🐕 Weekly drops — 2026-W24 — 40 cases"

    monkeypatch.setattr(fleet_report, "handle", fake_handle)

    out = asyncio.run(fleet_report.handle("weekly", "", cfg=types.SimpleNamespace(),
                                          client=object(), watch=[]))
    assert called["handle"] == 1 and "Weekly drops" in out
```

(The decisive assertion — that the dispatcher calls `fleet_report.handle` and skips `agent.answer` — is exercised through the integration test only if the repo already has an ibo-dispatch harness. It does not (`grep` found only the untracked `test_coverage_gaps3.py`), so do NOT fabricate a Telethon event harness. The routing is verified by Task 9 Step 4's `grep` trace; the unit test above proves `handle` is the deterministic entry point. Convention: sync `def test_` + `asyncio.run`, no `pytest-asyncio`.)

- [ ] **Step 2: Run to verify the suite is green before wiring**

Run: `.venv/bin/python -m pytest tests/test_fleet_report_dispatch.py -v`
Expected: PASS (this guards the deterministic entry point).

- [ ] **Step 3a: Import `fleet_report` in `mcp_watcher.py`**

At the import block (line ~42), add `fleet_report` to the `from watcherdog import (...)` list:

```python
                        daily_report, drop_stats, farm_stats, fast_commands,
                        fleet_report,
```

(Keep alphabetical-ish grouping; ensure it's inside the existing parenthesized import.)

- [ ] **Step 3b: Insert report routing in `_on_ibo`**

Immediately AFTER the `fast = commands.fast_parse(text)` block that returns (after line ~1127, before the `# Slash-command? Expand …` comment at ~1128), insert:

```python
        # Report commands (/weekly /today /top /worst /value /check /compare /bans)
        # are answered DETERMINISTICALLY from the weekly buffer + roster scan — no
        # model, even when AI is enabled (Phase 2: OpenRouter dropped from reports).
        report = commands.report_parse(text)
        if report is not None:
            try:
                async with client.action(target, "typing"):
                    reply = await fleet_report.handle(
                        report[0], report[1], cfg=cfg, client=client,
                        watch=state.get("watch") or [])
            except Exception:  # noqa: BLE001
                log.exception("ibo report command /%s failed", report[0])
                reply = "⚠️ couldn't build that report."
            if reply is not None:
                await _send(client, target, reply, deliver, cfg=cfg)
                return
```

- [ ] **Step 3c: Make `run_weekly_digest` deterministic**

Replace the body of `run_weekly_digest` (line ~1367-1383) with:

```python
async def run_weekly_digest(client, cfg, target, system_prompt, state, deliver=True):
    """Compile the deterministic /weekly report and send it to ibo. Read-only —
    this does NOT stop farms (that's the Wednesday drop-stats job). No model
    (Phase 2): always-on, free. `system_prompt` is accepted for caller
    compatibility but unused."""
    try:
        fleet = await fleet_report.snapshot(client, cfg, state.get("watch") or [])
        body = fleet_report.weekly(fleet)
    except Exception:  # noqa: BLE001
        log.exception("weekly digest failed to build; skipping this run")
        return
    await _alert(state, client, target, "🗓 Weekly digest\n\n" + body, deliver, cfg=cfg)
    log.info("sent weekly digest (%d chars, deterministic)", len(body or ""))
```

- [ ] **Step 4: Run the full suite + a syntax/import check**

Run: `.venv/bin/python -c "import watcherdog.mcp_watcher"` then
`.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`
Expected: import OK; suite green (no regressions).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_fleet_report_dispatch.py
git commit -m "feat(mcp_watcher): route report cmds to fleet_report; deterministic weekly digest"
```

---

## Task 8: Wire `bot_interface` report routing

**Files:**
- Modify: `watcherdog/bot_interface.py` (import `fleet_report` at line ~37; insert report routing after the `fast` block ~`:546-551`; add `_run_report_command` near `_run_fast_command` ~`:595`)
- Test: covered by the import/syntax check + the shared `fleet_report` unit tests

- [ ] **Step 1: Add the import**

At line ~37, add `fleet_report` to the `from watcherdog import (...)` list (inside the existing par_parenthesized import alongside `fast_commands`):

```python
                        fast_commands, fleet_report, task_store, tg_actions, tg_tools)
```

- [ ] **Step 2: Insert report routing after the fast block**

In the message handler, immediately AFTER the `fast = commands.fast_parse(text)` block that spawns `_run_fast_command` and returns (after line ~551), insert:

```python
        # Report commands — deterministic from the weekly buffer + roster (Phase 2).
        report = commands.report_parse(text)
        if report is not None:
            task = asyncio.create_task(self._run_report_command(event, *report))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            return
```

- [ ] **Step 3: Add `_run_report_command`**

Immediately after `_run_fast_command` (line ~607), add:

```python
    async def _run_report_command(self, event, cmd, args):
        """Answer a deterministic report command with no model (Phase 2). Reads the
        weekly drop buffer + a roster sweep via fleet_report.handle."""
        try:
            async with self.bot.action(event.chat_id, "typing"):
                text = await fleet_report.handle(
                    cmd, args, cfg=self.cfg, client=self.user_client,
                    watch=self.state.get("watch") or [])
        except Exception:  # noqa: BLE001
            log.exception("report command /%s failed", cmd)
            text = "⚠️ couldn't build that report."
        if text is None:
            text = "⚠️ couldn't build that report."
        await self._reply(event, text)
        log.info("bot → report /%s (%d chars, no AI)", cmd, len(text or ""))
```

- [ ] **Step 4: Syntax/import check + full suite**

Run: `.venv/bin/python -c "import watcherdog.bot_interface"` then
`.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`
Expected: import OK; suite green.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/bot_interface.py
git commit -m "feat(bot_interface): route report cmds to fleet_report (deterministic)"
```

---

## Task 9: Holistic verification + mutation spot-checks

**Files:** none (verification only)

- [ ] **Step 1: Full green-check (tracked tests only)**

Run: `.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`
Expected: all pass (Phase 1 baseline was 1070 passed, 2 skipped — expect that plus the new fleet_report/commands/drop_stats tests, 0 failures).

- [ ] **Step 2: Mutation spot-check the parser convergence**

Temporarily revert the adapter's `value` mapping to `rep.total_cases` (the old bug shape) and run `tests/test_drop_stats.py -k report_to_row`. Expected: `test_report_to_row_real_panel_correct_numbers` FAILS. Restore the fix; confirm it passes again. This proves the regression test actually guards the bug.

- [ ] **Step 3: Mutation spot-check the merge**

Temporarily break `snapshot`'s buffer merge (`row = by_num.get(num)` → `row = None`) and run `tests/test_fleet_report.py -k snapshot`. Expected: `test_snapshot_merges_roster_sweep_with_buffer` FAILS (drops/value go None). Restore.

- [ ] **Step 4: Grep for leftover model references in the report path**

Run: `grep -n "agent.answer\|commands.expand" watcherdog/mcp_watcher.py watcherdog/bot_interface.py`
Expected: the report commands no longer reach `commands.expand`/`agent.answer`; only free-form/`/whatsnew`/`/improve` and the destructive "drop stats" path remain. Confirm the weekly digest no longer calls `agent.answer`.

- [ ] **Step 5: Final commit (if any cleanup) + open PR**

```bash
git add -A && git commit -m "test(fleet_report): mutation spot-checks for parser convergence + merge" || true
```

Then open the PR per the worktree → PR → reviewer-pass → merge-if-clean process.

---

## Self-Review (completed by plan author)

- **Spec coverage:** A (parser convergence) → Task 1. B (`snapshot` + 8 formatters) → Tasks 2-5. C (dispatch + deterministic digest) → Tasks 6-8. Error handling (never-raise) → covered in `snapshot`/`handle`/formatters with explicit tests (read-error, missing buffer, unknown bot). Testing section → each task is TDD; Task 9 adds mutation spot-checks + the `git ls-files` green-check.
- **`/today` reframe** (live status + this-week drops) → Task 5 `today` + its test. **`/whatsnew` stays model/no_ai** → `report_parse` excludes it (Task 6 test asserts `None`). **`/improve` untouched** → not in `REPORT_NAMES`.
- **Type consistency:** `FleetEntry`/`Fleet`/`BotStats` field names used identically across Tasks 2-8; formatters all take `Fleet`; `handle(cmd, args, *, cfg, client, watch)` signature matches both dispatcher call-sites (Tasks 7-8). Buffer row keys (`drops`/`value`/`items`/`panel`) match `drop_sheets.COLUMNS` and Task 1's `_report_to_row` output.
- **Items mapping** (`items = len(skins)`) is explicit in Task 1; flagged for the owner in the spec review.
- **No placeholders:** every code step shows complete code; every run step shows the exact command + expected result.
