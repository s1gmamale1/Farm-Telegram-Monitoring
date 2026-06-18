# Wednesday Weekly Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Wednesday-00:00 scheduled job run a phased maintenance sequence — kill all farms → collect purple accounts → wait 1h → pull Drop Stats (waiting for the real DROP REPORT) → run the activity booster — while leaving the on-demand "drops stats" command a fast pull.

**Architecture:** Reshape the *scheduled* path only. `run_weekly` gains a `collector` parameter; the new `collect_maintenance` collector runs the global phases and feeds the existing buffer/Sheets/report tail. `weekly_loop` picks `collect_maintenance` when `cfg.weekly_maintenance_enabled` (default on), else the legacy `collect_week`. The on-demand command keeps calling `run_weekly` with the default `collect_week`. To stay under the 500-line rule, the pure parse/buffer/report helpers move to `watcherdog/drop_stats_format.py` and are re-exported from `drop_stats.py` (no public API change).

**Tech Stack:** Python 3.x, asyncio, Telethon (mocked in tests), pytest.

---

## File Structure

- **Create** `watcherdog/drop_stats_format.py` — pure helpers: `iso_week`, `buffer_path`, `panel_label`, `_report_to_row`, `make_row`, `write_buffer`, `load_buffer`, `format_report`.
- **Modify** `watcherdog/drop_stats.py` — re-export the moved helpers; add `PURPLE_BUTTONS`, `collect_purple`, `_await_reply(match=…)`, `request_drop_stats(wait_for_report=…)`, `_for_each_panel`, `collect_maintenance`; add `collector` param to `run_weekly`; collector selection in `weekly_loop`.
- **Modify** `watcherdog/panel_actions.py` — add `BTN_COLLECT_PURPLE`, `collect_purple`, register in `_ACTIONS`.
- **Modify** `watcherdog/config.py` — add `weekly_maintenance_enabled`, `purple_collect_wait_seconds`, `drop_report_timeout_seconds`.
- **Modify** `tests/test_drop_stats.py` — one fake signature update + new tests.
- **Modify/Create** `tests/test_panel_actions.py`, `tests/test_config.py` — new tests.

`mcp_watcher.py` is intentionally **not** changed (the gate lives in `weekly_loop` via `cfg`).

---

### Task 1: Extract pure helpers into `drop_stats_format.py` (refactor, no behavior change)

**Files:**
- Create: `watcherdog/drop_stats_format.py`
- Modify: `watcherdog/drop_stats.py` (remove the 8 pure helpers; add a re-export import)
- Test: existing `tests/test_drop_stats.py` (must stay green unchanged)

- [ ] **Step 1: Create `watcherdog/drop_stats_format.py`** by MOVING these functions verbatim from `drop_stats.py`: `iso_week`, `buffer_path`, `panel_label`, `_report_to_row`, `make_row`, `write_buffer`, `load_buffer`, `format_report`. Header:

```python
"""Pure (Telegram-free) helpers for the Drop Stats job: week ids, buffer file
I/O, report-row parsing (via farm_stats), and the ibo report renderer. Split out
of drop_stats.py so that module stays focused on panel-driving + scheduling and
under the 500-line limit. Re-exported from drop_stats for backward compatibility.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from watcherdog import farm_stats

log = logging.getLogger("watcherdog.drop_stats_format")

# <-- the 8 functions, moved verbatim, go here -->
```

- [ ] **Step 2: In `drop_stats.py`, delete those 8 functions** and their `# --- pure helpers ...` section comment. Immediately after the existing `from watcherdog import drop_sheets, farm_stats, tg_tools` import, add:

```python
from watcherdog.drop_stats_format import (  # re-exported pure helpers (back-compat)
    _report_to_row,
    buffer_path,
    format_report,
    iso_week,
    load_buffer,
    make_row,
    panel_label,
    write_buffer,
)
```

- [ ] **Step 3: Run the full drop_stats suite to verify no behavior change / no circular import**

Run: `python -m pytest tests/test_drop_stats.py -q`
Expected: PASS (same count as before this task).

- [ ] **Step 4: Commit**

```bash
git add watcherdog/drop_stats_format.py watcherdog/drop_stats.py
git commit -m "refactor(drop_stats): extract pure parse/buffer/report helpers into drop_stats_format"
```

---

### Task 2: Config knobs

**Files:**
- Modify: `watcherdog/config.py` (add three settings near `self.drop_stats_dir`, ~line 171)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py` (append):

```python
def test_weekly_maintenance_config_defaults():
    from watcherdog.config import Config
    cfg = Config({})
    assert cfg.weekly_maintenance_enabled is True
    assert cfg.purple_collect_wait_seconds == 3600.0
    assert cfg.drop_report_timeout_seconds == 1800.0


def test_weekly_maintenance_config_overrides():
    from watcherdog.config import Config
    cfg = Config({
        "WEEKLY_MAINTENANCE_ENABLED": "false",
        "PURPLE_COLLECT_WAIT_SECONDS": "10",
        "DROP_REPORT_TIMEOUT_SECONDS": "20",
    })
    assert cfg.weekly_maintenance_enabled is False
    assert cfg.purple_collect_wait_seconds == 10.0
    assert cfg.drop_report_timeout_seconds == 20.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_config.py::test_weekly_maintenance_config_defaults -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'weekly_maintenance_enabled'`

- [ ] **Step 3: Add the settings** in `config.py` right after the `self.drop_stats_dir = ...` line (~171):

```python
        # Weekly maintenance sequence (Wed 00:00): kill all -> collect purple ->
        # wait -> drop stats (await DROP REPORT) -> activity booster.
        self.weekly_maintenance_enabled = get("WEEKLY_MAINTENANCE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.purple_collect_wait_seconds = float(get("PURPLE_COLLECT_WAIT_SECONDS", "3600"))
        self.drop_report_timeout_seconds = float(get("DROP_REPORT_TIMEOUT_SECONDS", "1800"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_config.py -k weekly_maintenance -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/config.py tests/test_config.py
git commit -m "feat(config): add weekly-maintenance toggle + purple-wait + drop-report-timeout knobs"
```

---

### Task 3: `panel_actions.collect_purple`

**Files:**
- Modify: `watcherdog/panel_actions.py`
- Test: `tests/test_panel_actions.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_panel_actions.py`):

```python
def test_collect_purple_presses_the_purple_button(monkeypatch):
    import asyncio
    from watcherdog import panel_actions

    captured = {}

    async def fake_press_button(client, panel, label, **kw):
        captured["label"] = label
        return {"pressed": label}

    monkeypatch.setattr(panel_actions.tg_actions, "press_button", fake_press_button)
    res = asyncio.run(panel_actions.collect_purple(None, "panel"))
    assert res["ok"] is True
    assert captured["label"] == panel_actions.BTN_COLLECT_PURPLE
    assert "collect_purple" in panel_actions._ACTIONS
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_panel_actions.py::test_collect_purple_presses_the_purple_button -v`
Expected: FAIL with `AttributeError: module 'watcherdog.panel_actions' has no attribute 'collect_purple'`

- [ ] **Step 3: Implement.** Add the constant next to `BTN_ACTIVITY_BOOSTER` (line 19):

```python
BTN_COLLECT_PURPLE = "collect purple"
```

Add the action after `run_activity_booster` (after line 49):

```python
async def collect_purple(client, panel):
    return await _press(client, panel, BTN_COLLECT_PURPLE)
```

Register it in `_ACTIONS` (in the dict at line 62):

```python
    "collect_purple": lambda c, p, cf: collect_purple(c, p),
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_panel_actions.py::test_collect_purple_presses_the_purple_button -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add watcherdog/panel_actions.py tests/test_panel_actions.py
git commit -m "feat(panel_actions): add collect_purple named action"
```

---

### Task 4: drop_stats — purple driver, report-aware wait

**Files:**
- Modify: `watcherdog/drop_stats.py`
- Test: `tests/test_drop_stats.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_drop_stats.py`):

```python
def test_await_reply_match_filters_to_matching_message():
    import asyncio
    from types import SimpleNamespace

    nope = SimpleNamespace(out=False, id=11, buttons=None, message="just a reply")
    report = SimpleNamespace(out=False, id=12, buttons=None, message="x DROP REPORT x")

    class _Client:
        async def get_messages(self, ent, limit=6):
            return [report, nope]  # newest first

    result = asyncio.run(drop_stats._await_reply(
        _Client(), "ent", after_id=5, timeout=1.0,
        match=lambda m: "drop report" in (m.message or "").lower()))
    assert result is report


def test_collect_purple_returns_false_when_no_menu(monkeypatch):
    import asyncio

    async def fake_open_menu(client, ent, **kw):
        return None

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    assert asyncio.run(drop_stats.collect_purple(None, "ent")) is False


def test_collect_purple_presses_purple_button(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    seen = {}

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]], id=1)

    async def fake_press(msg, prefixes):
        seen["prefixes"] = prefixes
        return True

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    assert asyncio.run(drop_stats.collect_purple(None, "ent")) is True
    assert seen["prefixes"] == drop_stats.PURPLE_BUTTONS


def test_request_drop_stats_default_passes_no_match(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]], id=5)

    async def fake_press(msg, prefixes):
        return True

    captured = {}

    async def fake_await_reply(client, ent, after_id, *, match=None, **kw):
        captured["match"] = match
        return SimpleNamespace(message="312 drops")

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    monkeypatch.setattr(drop_stats, "_await_reply", fake_await_reply)
    out = asyncio.run(drop_stats.request_drop_stats(None, "ent"))
    assert "312" in out
    assert captured["match"] is None


def test_request_drop_stats_wait_for_report_filters_to_title(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    async def fake_open_menu(client, ent, **kw):
        return SimpleNamespace(buttons=[[]], id=5)

    async def fake_press(msg, prefixes):
        return True

    async def fake_await_reply(client, ent, after_id, *, match=None, **kw):
        assert match is not None
        assert match(SimpleNamespace(message="some other reply")) is False
        assert match(SimpleNamespace(message="=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=")) is True
        return SimpleNamespace(message="=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=\nTotal cases: 5 pcs.")

    monkeypatch.setattr(drop_stats, "_open_menu", fake_open_menu)
    monkeypatch.setattr(drop_stats, "_press", fake_press)
    monkeypatch.setattr(drop_stats, "_await_reply", fake_await_reply)
    out = asyncio.run(drop_stats.request_drop_stats(None, "ent", wait_for_report=True, timeout=2.0))
    assert "DROP REPORT" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_drop_stats.py -k "collect_purple or wait_for_report or match_filters or default_passes_no_match" -v`
Expected: FAIL (PURPLE_BUTTONS / collect_purple / match kwarg missing)

- [ ] **Step 3: Implement.** Add after `BOOSTER_BUTTONS` (line 40):

```python
# Operator weekly maintenance: collect "purple" accounts before pulling stats.
PURPLE_BUTTONS = ("collect purple", "purple")
```

Change the `_await_reply` signature (line 222) to accept a `match` predicate, and add the predicate check just before `return m` inside the loop:

```python
async def _await_reply(client, ent, after_id, *, need_buttons=False, match=None,
                       timeout=20.0, poll=1.5):
```

```python
            if need_buttons and not getattr(m, "buttons", None):
                continue
            if match is not None and not match(m):
                continue
            return m
```

Replace `request_drop_stats` (lines 278-287) with:

```python
async def request_drop_stats(client, ent, *, timeout=25.0, wait_for_report=False):
    """Press *Drops Stats* and return the reply text ("" if unavailable).

    With ``wait_for_report=True`` it waits (up to ``timeout``) for the panel's
    DROP REPORT message specifically — a reply whose text contains "drop report"
    (case-insensitive, emoji-safe) — instead of the first reply. Used by the
    scheduled weekly maintenance run, whose report can take minutes to arrive.
    """
    menu = await _open_menu(client, ent)
    if menu is None:
        return ""
    if not await _press(menu, DROPS_BUTTONS):
        log.warning("%s: no Drops Stats button found", tg_tools.entity_name(ent))
        return ""
    match = (lambda m: "drop report" in (m.message or "").lower()) if wait_for_report else None
    reply = await _await_reply(client, ent, menu.id, timeout=timeout, match=match)
    return (reply.message or "") if reply else ""
```

Add `collect_purple` after `run_activity_booster` (after line 303):

```python
async def collect_purple(client, ent):
    """Open the panel's menu and press *Collect purple accounts*. True if pressed."""
    menu = await _open_menu(client, ent)
    if menu is None:
        log.warning("%s: no /start menu — cannot collect purple", tg_tools.entity_name(ent))
        return False
    pressed = await _press(menu, PURPLE_BUTTONS)
    if not pressed:
        log.warning("%s: no collect-purple button found", tg_tools.entity_name(ent))
    return pressed
```

- [ ] **Step 4: Run to verify they pass (and nothing regressed)**

Run: `python -m pytest tests/test_drop_stats.py -q`
Expected: PASS (old tests + the 5 new ones)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/drop_stats.py tests/test_drop_stats.py
git commit -m "feat(drop_stats): purple driver + report-aware Drop Stats wait"
```

---

### Task 5: drop_stats — `collect_maintenance` phased collector

**Files:**
- Modify: `watcherdog/drop_stats.py`
- Test: `tests/test_drop_stats.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_drop_stats.py`):

```python
def test_collect_maintenance_global_phase_order(monkeypatch):
    """kill-all-panels -> purple-all -> sleep(1h) -> drop-all -> booster-all."""
    import asyncio

    calls = []

    async def fake_stop(client, ent):
        calls.append(("kill", ent)); return True

    async def fake_purple(client, ent):
        calls.append(("purple", ent)); return True

    async def fake_drops(client, ent, **kw):
        calls.append(("drops", ent, kw.get("wait_for_report"))); return "DROP REPORT\n..."

    async def fake_boost(client, ent):
        calls.append(("boost", ent)); return True

    async def fake_sleep(s):
        calls.append(("sleep", s))

    monkeypatch.setattr(drop_stats, "stop_farm", fake_stop)
    monkeypatch.setattr(drop_stats, "collect_purple", fake_purple)
    monkeypatch.setattr(drop_stats, "request_drop_stats", fake_drops)
    monkeypatch.setattr(drop_stats, "run_activity_booster", fake_boost)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", fake_sleep)

    cfg = Config({"PURPLE_COLLECT_WAIT_SECONDS": "3600"})
    panels = [("Panel 1", "ent1"), ("Panel 2", "ent2")]
    rows = asyncio.run(drop_stats.collect_maintenance(
        None, cfg, panels, week="2026-W23", date="2026-06-03"))

    assert calls == [
        ("kill", "ent1"), ("kill", "ent2"),
        ("purple", "ent1"), ("purple", "ent2"),
        ("sleep", 3600.0),
        ("drops", "ent1", True), ("drops", "ent2", True),
        ("boost", "ent1"), ("boost", "ent2"),
    ]
    assert len(rows) == 2


def test_collect_maintenance_dry_run_presses_nothing_and_does_not_sleep(monkeypatch):
    import asyncio

    calls = []

    async def boom(*a, **k):
        calls.append("press"); return True

    async def boom_sleep(s):
        calls.append("sleep")

    monkeypatch.setattr(drop_stats, "stop_farm", boom)
    monkeypatch.setattr(drop_stats, "collect_purple", boom)
    monkeypatch.setattr(drop_stats, "request_drop_stats", boom)
    monkeypatch.setattr(drop_stats, "run_activity_booster", boom)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", boom_sleep)

    panels = [("Panel 1", "e1"), ("Panel 2", "e2")]
    rows = asyncio.run(drop_stats.collect_maintenance(
        None, Config({}), panels, week="2026-W23", date="d", deliver=False))
    assert calls == []
    assert len(rows) == 2
    assert all(r["notes"] == "dry-run" for r in rows)


def test_collect_maintenance_no_report_marks_notes(monkeypatch):
    import asyncio

    async def ok(client, ent, *a, **k):
        return True

    async def empty_drops(client, ent, **kw):
        return "   "

    async def fake_sleep(s):
        return None

    monkeypatch.setattr(drop_stats, "stop_farm", ok)
    monkeypatch.setattr(drop_stats, "collect_purple", ok)
    monkeypatch.setattr(drop_stats, "request_drop_stats", empty_drops)
    monkeypatch.setattr(drop_stats, "run_activity_booster", ok)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", fake_sleep)

    rows = asyncio.run(drop_stats.collect_maintenance(
        None, Config({}), [("Panel 1", "e1")], week="2026-W23", date="d"))
    assert rows[0]["notes"] == "no report"


def test_collect_maintenance_one_bad_panel_does_not_abort_rest(monkeypatch):
    import asyncio

    async def kill_one_raises(client, ent):
        if ent == "e1":
            raise RuntimeError("panel 1 down")
        return True

    async def ok(client, ent, *a, **k):
        return True

    async def drops(client, ent, **kw):
        return "DROP REPORT\nTotal cases: 1 pcs."

    async def fake_sleep(s):
        return None

    monkeypatch.setattr(drop_stats, "stop_farm", kill_one_raises)
    monkeypatch.setattr(drop_stats, "collect_purple", ok)
    monkeypatch.setattr(drop_stats, "request_drop_stats", drops)
    monkeypatch.setattr(drop_stats, "run_activity_booster", ok)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", fake_sleep)

    rows = asyncio.run(drop_stats.collect_maintenance(
        None, Config({}), [("Panel 1", "e1"), ("Panel 2", "e2")],
        week="2026-W23", date="d"))
    assert len(rows) == 2  # the bad kill on e1 did not abort the run
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_drop_stats.py -k collect_maintenance -v`
Expected: FAIL with `AttributeError: ... has no attribute 'collect_maintenance'`

- [ ] **Step 3: Implement.** Add after `collect_week` (after line 346):

```python
async def _for_each_panel(client, panels, action, label):
    """Run ``action(client, ent)`` on every panel; log (never raise) per-panel
    failures so one slow/dead panel never blocks the rest of the phase."""
    for name, ent in panels:
        try:
            await action(client, ent)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: %s failed: %s", panel_label(name), label, exc)


async def collect_maintenance(client, cfg, panels, *, week, date=None, deliver=True):
    """Weekly maintenance collector (global phases): kill ALL farms -> collect
    purple on ALL -> wait ``purple_collect_wait_seconds`` -> Drop Stats on ALL
    (awaiting each panel's DROP REPORT) -> activity booster on ALL. Returns one
    row per panel. ``deliver=False`` presses nothing and does not sleep."""
    if not deliver:
        log.info("[DRY-RUN] weekly maintenance over %d panels: "
                 "kill->purple->wait->drop->booster", len(panels))
        rows = []
        for name, ent in panels:
            parsed = _report_to_row("")
            parsed["notes"] = "dry-run"
            rows.append(make_row(week, panel_label(name), parsed, date=date))
        return rows

    await _for_each_panel(client, panels, stop_farm, "kill all")
    await _for_each_panel(client, panels, collect_purple, "collect purple")
    await asyncio.sleep(cfg.purple_collect_wait_seconds)

    rows = []
    for name, ent in panels:
        panel = panel_label(name)
        text = ""
        try:
            text = await request_drop_stats(
                client, ent, wait_for_report=True,
                timeout=cfg.drop_report_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: drop-stats request failed: %s", panel, exc)
        parsed = _report_to_row(text)
        if not text.strip():
            parsed["notes"] = "no report"
        rows.append(make_row(week, panel, parsed, date=date))

    await _for_each_panel(client, panels, run_activity_booster, "activity booster")
    return rows
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_drop_stats.py -k collect_maintenance -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/drop_stats.py tests/test_drop_stats.py
git commit -m "feat(drop_stats): collect_maintenance phased weekly collector"
```

---

### Task 6: Wire the scheduled path to the maintenance collector

**Files:**
- Modify: `watcherdog/drop_stats.py` (`run_weekly` gains `collector`; `weekly_loop` selects it)
- Test: `tests/test_drop_stats.py` (update one fake signature; add a selection test)

- [ ] **Step 1: Write the failing test + update the existing one.**

Update `test_weekly_loop_alerts_once_per_failure_streak`: change its `fake_run_weekly` signature to accept the new kwarg:

```python
    async def fake_run_weekly(client, cfg, target=None, *, deliver=True, collector=None):
```

Append a new test:

```python
def test_weekly_loop_selects_maintenance_collector_when_enabled(monkeypatch):
    import asyncio
    import asyncio as _aio

    seen = {}
    n = {"i": 0}

    async def fake_run_weekly(client, cfg, target=None, *, deliver=True, collector=None):
        seen["collector"] = collector
        return {"ok": True}

    async def fake_sleep(s):
        n["i"] += 1
        if n["i"] >= 2:        # initial seconds_until sleep, then post-success 60s
            raise _aio.CancelledError()

    monkeypatch.setattr(drop_stats, "run_weekly", fake_run_weekly)
    monkeypatch.setattr(drop_stats, "seconds_until", lambda *a, **k: 0)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", fake_sleep)

    try:
        asyncio.run(drop_stats.weekly_loop(object(), Config({}), target="ibo"))
    except _aio.CancelledError:
        pass
    assert seen["collector"] is drop_stats.collect_maintenance


def test_weekly_loop_uses_legacy_collector_when_disabled(monkeypatch):
    import asyncio
    import asyncio as _aio

    seen = {}
    n = {"i": 0}

    async def fake_run_weekly(client, cfg, target=None, *, deliver=True, collector=None):
        seen["collector"] = collector
        return {"ok": True}

    async def fake_sleep(s):
        n["i"] += 1
        if n["i"] >= 2:
            raise _aio.CancelledError()

    monkeypatch.setattr(drop_stats, "run_weekly", fake_run_weekly)
    monkeypatch.setattr(drop_stats, "seconds_until", lambda *a, **k: 0)
    monkeypatch.setattr(drop_stats.asyncio, "sleep", fake_sleep)

    try:
        asyncio.run(drop_stats.weekly_loop(
            object(), Config({"WEEKLY_MAINTENANCE_ENABLED": "false"}), target="ibo"))
    except _aio.CancelledError:
        pass
    assert seen["collector"] is drop_stats.collect_week
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python -m pytest tests/test_drop_stats.py -k "weekly_loop_selects or weekly_loop_uses_legacy" -v`
Expected: FAIL (`run_weekly` has no `collector` kwarg / weekly_loop ignores it)

- [ ] **Step 3: Implement.** In `run_weekly` (line 370), add the param and use it:

```python
async def run_weekly(client, cfg, target=None, *, deliver=True, now=None, collector=None):
```

Just before the `rows = await collect_week(...)` call (line 393), select the collector and call it:

```python
    collect = collector or collect_week
    rows = await collect(client, cfg, panels, week=week, date=now.date().isoformat(),
                         deliver=deliver)
```

(Replace the existing `rows = await collect_week(...)` line.)

In `weekly_loop` (line 408), pick the collector once and thread it through both run calls:

```python
async def weekly_loop(client, cfg, target=None, *, deliver=True):
    collector = collect_maintenance if getattr(cfg, "weekly_maintenance_enabled", True) else collect_week
```

and change the `res = await run_weekly(...)` call (line 427) to:

```python
                res = await run_weekly(client, cfg,
                                       target if not alerted else None,
                                       deliver=deliver, collector=collector)
```

- [ ] **Step 4: Run to verify all pass**

Run: `python -m pytest tests/test_drop_stats.py -q`
Expected: PASS (including the updated alert-once test and both new selection tests)

- [ ] **Step 5: Commit**

```bash
git add watcherdog/drop_stats.py tests/test_drop_stats.py
git commit -m "feat(drop_stats): schedule the maintenance collector (gated by cfg)"
```

---

### Task 7: Full-suite + line-count verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions across the repo).

- [ ] **Step 2: Verify the 500-line rule holds**

Run: `wc -l watcherdog/drop_stats.py watcherdog/drop_stats_format.py`
Expected: both under 500. (If `drop_stats.py` is still ≥ 500, move any remaining pure helper not yet relocated into `drop_stats_format.py` and re-export it.)

- [ ] **Step 3: Commit (only if anything changed in Step 2)**

```bash
git add -A && git commit -m "chore(drop_stats): keep modules under the 500-line limit"
```

---

## Self-Review

**Spec coverage:**
- Kill all farms (every panel) → `collect_maintenance` Phase 1 `stop_farm` via `_for_each_panel`. ✓
- Collect purple (new action) → `panel_actions.collect_purple` (Task 3) + `drop_stats.collect_purple` (Task 4) + Phase 2. ✓
- Wait 1h → `asyncio.sleep(cfg.purple_collect_wait_seconds)` (Phase 3), tested via patched sleep + dry-run skip. ✓
- Drop Stats awaiting DROP REPORT title → `request_drop_stats(wait_for_report=True)` + `_await_reply(match=…)`. ✓
- Activity booster last → Phase 5. ✓
- Reshape scheduled job, keep on-demand fast → `collector` param + `weekly_loop` selection; `run_weekly` default stays `collect_week`; on-demand caller untouched. ✓
- Config knobs → Task 2. ✓
- Sheets left as-is → `run_weekly` tail unchanged. ✓
- 500-line rule → Task 1 extraction + Task 7 check. ✓
- Zero-panel guard preserved → `run_weekly` guard unchanged (collector only swaps the collect step). ✓

**Placeholder scan:** none — every code/test step has full code and exact commands.

**Type/name consistency:** `collect_maintenance` / `collect_week` share signature `(client, cfg, panels, *, week, date=None, deliver=True) -> rows`; `run_weekly(..., collector=None)`; `_await_reply(..., match=None, ...)`; `request_drop_stats(..., wait_for_report=False)`; `PURPLE_BUTTONS`; `BTN_COLLECT_PURPLE`. Consistent across tasks.

**Risks (carried from spec, verify live, not blocking):** real purple button label (emoji prefix) — `panel_actions.collect_purple` is substring-tolerant; `drop_stats.collect_purple` uses `startswith` like the other drivers; capture a real menu to confirm. Kill-All confirm dialog — verify whether a confirm press is needed.
