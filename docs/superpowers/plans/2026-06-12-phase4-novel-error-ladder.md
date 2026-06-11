# Phase 4 — Novel-Error Action Ladder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This run: INLINE execution (executing-plans).**

**Goal:** Novel errors (no learned fix) get a deterministic generic-restart ladder with attempt tracking + escalation, flagged `novel=1` on IncidentTracker; `_incident_via_agent` (the last model call reachable from the monitor loop) is deleted.

**Architecture:** New pure-ish module `novel_recovery.attempt()` gates (critical-family → human; capability/flag → skip) then runs `panel_actions.run_sequence(["kill_all","select_unfarmed","start_selected"])`. `_evaluate_bot`'s final branch calls it **only when `fix_status is None`** (truly novel); retries are paced by the existing incident refix loop (`fixable=1` + `incident_max_fix_retries` budget), which routes `novel=1` rows to the ladder instead of `auto_fix`.

**Tech Stack:** Python 3.14, stdlib sqlite3, Telethon (passed-in), pytest. Venv: `.venv/bin/python`. Green-check: `.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`. Async tests: sync `def test_` + `asyncio.run(...)` (NO pytest-asyncio). Tracker tests use a REAL `IncidentTracker` (fake-objects lesson).

---

## File Structure

| File | Responsibility |
|---|---|
| `watcherdog/novel_recovery.py` | **new** — `attempt()` + `LADDER` (~70 ln) |
| `watcherdog/incident_tracker.py` | `novel` column (CREATE + guarded ALTER migration), `open(novel=)` w/ legacy-INSERT fallback, `novel_list()` |
| `watcherdog/alerter.py` | `format_novel_alert` |
| `watcherdog/config.py` | `NOVEL_RECOVERY` flag (default on) |
| `watcherdog/mcp_watcher.py` | rewire `_evaluate_bot:930-940`; delete `_incident_via_agent:268-298`; `_open_bot_incident(novel=)`; refix-tick routing |
| `tests/test_novel_recovery.py` | **new** — attempt outcomes |
| `tests/test_incident_tracker.py` | append migration + novel tests |
| `tests/test_alerter.py` (if exists; else into test_novel_recovery.py) | format_novel_alert |

---

## Task 1: IncidentTracker — `novel` column, migration, `open(novel=)`, `novel_list()`

**Files:** Modify `watcherdog/incident_tracker.py:34-104`; Test `tests/test_incident_tracker.py` (append).

- [ ] **Step 1: Failing tests** — append to `tests/test_incident_tracker.py`:

```python
# --- Phase 4: novel flag + migration -----------------------------------------

def test_migration_adds_novel_column_to_old_db(tmp_path):
    """A pre-Phase-4 DB (no `novel` column) upgrades in place; old rows read novel=0."""
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE open_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
            source TEXT NOT NULL, bot TEXT NOT NULL, severity TEXT, summary TEXT,
            raw_excerpt TEXT, fixable INTEGER NOT NULL DEFAULT 0,
            fix_attempted TEXT, fix_retries INTEGER NOT NULL DEFAULT 0,
            opened_ts REAL NOT NULL, last_update_ts REAL NOT NULL,
            update_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open', resolved_ts REAL, resolution TEXT)
    """)
    conn.execute(
        "INSERT INTO open_incidents (key, source, bot, severity, summary, fixable,"
        " opened_ts, last_update_ts, update_count, status)"
        " VALUES ('bot_error:SF1','bot_error','SF1','high','old row',0,1.0,1.0,0,'open')")
    conn.commit(); conn.close()
    t = IncidentTracker(db)
    row = t.open_for_bot("bot_error", "SF1")
    assert row["novel"] == 0                 # old row readable, defaulted
    assert t.novel_list() == []
    t.close()


def test_open_novel_flag_and_novel_list(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird new error",
           fixable=True, novel=True, now=100.0)
    t.open("bot_error", "SF8", "bot_error:SF8", "high", "known error",
           fixable=True, now=101.0)          # default novel=False
    novel = t.novel_list()
    assert [r["bot"] for r in novel] == ["SF7"]
    assert novel[0]["novel"] == 1
    assert t.open_for_bot("bot_error", "SF8")["novel"] == 0
    t.close()
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_incident_tracker.py -k "novel or migration" -v` → FAIL (`no such column: novel` / unexpected kwarg / no attribute `novel_list`).

- [ ] **Step 3: Implement.** In `_init_schema`: add `novel INTEGER NOT NULL DEFAULT 0,` to the CREATE TABLE column list (after `fixable`), and after the CREATE add the guarded migration:

```python
        # Phase 4 migration: pre-existing DBs gain the `novel` flag in place.
        try:
            self.conn.execute(
                "ALTER TABLE open_incidents ADD COLUMN novel INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists (new DB or already migrated)
```

`open()` gains `novel=False` after `fixable`; INSERT lists `novel` and binds `1 if novel else 0`; wrap the INSERT so a weird un-migrated DB never crashes the watcher:

```python
        try:
            self.conn.execute(
                """
                INSERT INTO open_incidents
                    (key, source, bot, severity, summary, raw_excerpt, fixable,
                     novel, fix_attempted, fix_retries, opened_ts, last_update_ts,
                     update_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'open')
                """,
                (key, source, bot, severity, summary, raw_excerpt,
                 1 if fixable else 0, 1 if novel else 0, fix_attempted, now, now),
            )
        except sqlite3.OperationalError:
            # `novel` column absent (migration failed on an exotic DB): degrade to
            # the legacy insert rather than crash the watcher.
            self.conn.execute(
                """
                INSERT INTO open_incidents
                    (key, source, bot, severity, summary, raw_excerpt, fixable,
                     fix_attempted, fix_retries, opened_ts, last_update_ts,
                     update_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'open')
                """,
                (key, source, bot, severity, summary, raw_excerpt,
                 1 if fixable else 0, fix_attempted, now, now),
            )
```

Add accessor after `open_list`:

```python
    def novel_list(self):
        """Open incidents flagged novel (the Phase 5 overseer queue), oldest first."""
        try:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM open_incidents WHERE status = 'open' AND novel = 1 "
                "ORDER BY opened_ts").fetchall()]
        except sqlite3.OperationalError:
            return []
```

(`import sqlite3` is already at module top.)

- [ ] **Step 4: Run, verify pass** — same `-k` selection, then the whole file: `pytest tests/test_incident_tracker.py -q`.
- [ ] **Step 5: Mutation-verify** — temporarily bind `0` instead of `1 if novel else 0` in the INSERT → `test_open_novel_flag_and_novel_list` FAILS; restore → passes.
- [ ] **Step 6: Commit** — `git add watcherdog/incident_tracker.py tests/test_incident_tracker.py && git commit -m "feat(incidents): novel flag + in-place migration + novel_list()"`

---

## Task 2: `novel_recovery.attempt()`

**Files:** Create `watcherdog/novel_recovery.py`; Create `tests/test_novel_recovery.py`.

- [ ] **Step 1: Failing tests** — create `tests/test_novel_recovery.py`:

```python
"""Tests for watcherdog.novel_recovery — the Phase 4 generic-restart ladder."""

from __future__ import annotations

import asyncio
import types

from watcherdog import novel_recovery


def _cfg(**kw):
    base = dict(novel_recovery=True, agent_actions_enabled=True,
                daily_errors_path=None, panel_settle_seconds=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(coro):
    return asyncio.run(coro)


def test_critical_family_is_human_needed_and_never_presses(monkeypatch):
    called = []
    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}] * 3
    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] account banned by VAC",
                                      chat=object(), deliver=True))
    assert out["status"] == "human_needed"
    assert called == []                      # the gate runs FIRST — no press


def test_flag_off_dryrun_and_actions_off_all_skip(monkeypatch):
    called = []
    async def fake_seq(*a, **k):
        called.append(a)
        return [{"ok": True}] * 3
    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    novel_text = "[SinFermera7] flux capacitor desync"   # not critical-family
    for cfg, deliver in ((_cfg(novel_recovery=False), True),
                         (_cfg(), False),
                         (_cfg(agent_actions_enabled=False), True)):
        out = _run(novel_recovery.attempt(object(), cfg, "SF7", novel_text,
                                          chat=object(), deliver=deliver))
        assert out["status"] == "skipped"
    assert called == []


def test_happy_ladder_attempted_in_order(monkeypatch):
    seen = {}
    async def fake_seq(client, panel, actions, cfg, *, confirmed=False):
        seen["actions"] = list(actions); seen["confirmed"] = confirmed
        return [{"ok": True}] * len(actions)
    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "attempted"
    assert seen["actions"] == ["kill_all", "select_unfarmed", "start_selected"]
    assert seen["confirmed"] is True


def test_mid_ladder_failure_reports_step(monkeypatch):
    async def fake_seq(client, panel, actions, cfg, *, confirmed=False):
        return [{"ok": True}, {"ok": False, "detail": {"error": "timeout"}}]
    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", fake_seq)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "failed"
    assert out["failed_step"] == "select_unfarmed"


def test_attempt_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("telethon died")
    monkeypatch.setattr(novel_recovery.panel_actions, "run_sequence", boom)
    out = _run(novel_recovery.attempt(object(), _cfg(), "SF7",
                                      "[SinFermera7] flux capacitor desync",
                                      chat=object(), deliver=True))
    assert out["status"] == "failed"
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_novel_recovery.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — create `watcherdog/novel_recovery.py`:

```python
"""Deterministic recovery for NOVEL errors (Phase 4) — no model.

When an error has no learned fix, run the generic restart ladder the panel
rules already trust (kill_all -> select_unfarmed -> start_selected) instead of
asking a model to improvise. Critical-family errors (ban / captcha / Steam
Guard) are never auto-pressed — a restart can't fix them. The caller opens a
``novel=1`` incident either way; retries are paced by the incident follow-up
loop within the existing ``incident_max_fix_retries`` budget.
"""

from __future__ import annotations

import logging

from watcherdog import daily_report, panel_actions
from watcherdog.classifier import severity_of

logger = logging.getLogger("watcherdog.novel_recovery")

LADDER = ("kill_all", "select_unfarmed", "start_selected")


async def attempt(client, cfg, bot, text, *, chat=None, deliver=True):
    """Try the generic restart ladder on a novel error. Never raises.

    Returns ``{"status": "human_needed"|"skipped"|"attempted"|"failed", ...}``.
    The critical-family gate runs FIRST — even on a dry run — because a ban /
    captcha / Steam Guard prompt is a human problem regardless of capabilities.
    """
    if severity_of(text) == "critical":
        return {"status": "human_needed"}
    if not getattr(cfg, "novel_recovery", True):
        return {"status": "skipped", "reason": "NOVEL_RECOVERY off"}
    if not (getattr(cfg, "agent_actions_enabled", False) and deliver):
        return {"status": "skipped", "reason": "actions disabled / dry-run"}
    target = chat if chat is not None else bot
    try:
        results = await panel_actions.run_sequence(
            client, target, list(LADDER), cfg, confirmed=True)
    except Exception:  # noqa: BLE001
        logger.exception("novel ladder raised for %s", bot)
        results = [{"ok": False, "detail": {"error": "exception"}}]
    ok = len(results) == len(LADDER) and all(r.get("ok") for r in results)
    fix_desc = " -> ".join(LADDER)
    summary = " ".join((text or "").split())[:80]
    daily_report.record(getattr(cfg, "daily_errors_path", None), panel=bot,
                        error=f"novel: {summary}", fix=fix_desc,
                        result="ok" if ok else "failed")
    if ok:
        logger.info("NOVEL-LADDER %s — %s (no AI)", bot, fix_desc)
        return {"status": "attempted", "steps": list(LADDER), "results": results}
    failed_step = LADDER[max(0, len(results) - 1)] if results else LADDER[0]
    logger.warning("NOVEL-LADDER %s — failed at %s", bot, failed_step)
    return {"status": "failed", "steps": list(LADDER), "results": results,
            "failed_step": failed_step}
```

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_novel_recovery.py -v` (5 pass).
- [ ] **Step 5: Mutation-verify** — flip the critical gate to `!=` → `test_critical_family...` FAILS; restore.
- [ ] **Step 6: Commit** — `git add watcherdog/novel_recovery.py tests/test_novel_recovery.py && git commit -m "feat(novel_recovery): deterministic generic-restart ladder for novel errors"`

---

## Task 3: `alerter.format_novel_alert`

**Files:** Modify `watcherdog/alerter.py` (after `format_alert`, line ~58); Test: append to `tests/test_novel_recovery.py` (keeps Phase 4 tests together; `tests/test_alerter.py` may not exist).

- [ ] **Step 1: Failing tests** — append:

```python
from watcherdog.alerter import format_novel_alert


def test_format_novel_alert_attempted_line():
    out = format_novel_alert("SF7", "high", {"summary": "weird"}, "raw",
                             {"status": "attempted"})
    assert "generic restart" in out and "verify next sweep" in out


def test_format_novel_alert_failed_names_step():
    out = format_novel_alert("SF7", "high", {"summary": "weird"}, "raw",
                             {"status": "failed", "failed_step": "kill_all"})
    assert "FAILED at 'kill_all'" in out


def test_format_novel_alert_human_needed_and_skipped():
    human = format_novel_alert("SF7", "critical", {}, "banned", {"status": "human_needed"})
    assert "not auto-restarting" in human
    plain = format_novel_alert("SF7", "high", {}, "raw", {"status": "skipped"})
    assert "🛠" not in plain and "🚫" not in plain      # plain alert, no recovery line
```

- [ ] **Step 2: Run, verify fail** — ImportError.

- [ ] **Step 3: Implement** — add to `watcherdog/alerter.py` directly after `format_alert`:

```python
def format_novel_alert(bot_name, severity, analysis, raw_excerpt, recovery):
    """``format_alert`` plus one deterministic-recovery line (Phase 4).

    ``recovery`` is the ``novel_recovery.attempt`` outcome; ``skipped`` renders
    the plain alert unchanged."""
    base = format_alert(bot_name, severity, analysis, raw_excerpt)
    status = (recovery or {}).get("status")
    if status == "attempted":
        line = ("🛠 Novel error — ran the generic restart "
                "(kill all → relaunch); will verify next sweep.")
    elif status == "failed":
        step = (recovery or {}).get("failed_step", "?")
        line = f"🛠 Novel error — generic restart FAILED at '{step}'. Needs you."
    elif status == "human_needed":
        line = "🚫 Novel error in the ban/captcha class — not auto-restarting. Needs you."
    else:
        return base
    msg = f"{base}\n\n{line}"
    return msg[:4000]   # same Telegram safety cap as format_alert
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git add watcherdog/alerter.py tests/test_novel_recovery.py && git commit -m "feat(alerter): format_novel_alert recovery line"`

---

## Task 4: Config flag `NOVEL_RECOVERY`

**Files:** Modify `watcherdog/config.py` (next to `agent_actions_enabled`, ~line 396); Test: `tests/test_config_defaults.py` (append, mirroring its delenv style).

- [ ] **Step 1: Failing test** — append to `tests/test_config_defaults.py` (match the file's existing fixture/delenv pattern — read its first test and mirror it):

```python
def test_novel_recovery_defaults_on(monkeypatch):
    monkeypatch.delenv("NOVEL_RECOVERY", raising=False)
    cfg = _fresh_config(monkeypatch)   # use the file's existing helper; if it has
                                       # none, construct Config the same way its
                                       # other default tests do
    assert cfg.novel_recovery is True
```

- [ ] **Step 2: Run, verify fail** — AttributeError.
- [ ] **Step 3: Implement** — in `config.py`, next to the other action flags:

```python
        # Phase 4: deterministic generic-restart ladder for NOVEL errors (errors
        # with no learned fix). On by default; the critical (ban/captcha) family
        # is always exempt regardless of this flag.
        self.novel_recovery = get("NOVEL_RECOVERY", "true").strip().lower() in ("1", "true", "yes")
```

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_config_defaults.py -q`.
- [ ] **Step 5: Commit** — `git add watcherdog/config.py tests/test_config_defaults.py && git commit -m "feat(config): NOVEL_RECOVERY flag (default on)"`

---

## Task 5: Wire `_evaluate_bot` + delete `_incident_via_agent` + `_open_bot_incident(novel=)`

**Files:** Modify `watcherdog/mcp_watcher.py` (`:268-298` delete; `:748` kwarg; `:930-940` rewire; import `novel_recovery` at `:41-58`; import `format_novel_alert` next to `format_alert` in the alerter import block); Test: append to `tests/test_novel_recovery.py`.

- [ ] **Step 1: Failing test** — `_open_bot_incident` passthrough with a REAL tracker:

```python
def test_open_bot_incident_passes_novel_flag(tmp_path):
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    state = {"tracker": t}
    mcp_watcher._open_bot_incident(state, "SF7", "high", {"summary": "weird"},
                                   "raw text", fixable=True, novel=True, now=100.0)
    row = t.open_for_bot("bot_error", "SF7")
    assert row["novel"] == 1 and row["fixable"] == 1
    t.close()
```

- [ ] **Step 2: Run, verify fail** — `TypeError: unexpected keyword argument 'novel'`.

- [ ] **Step 3: Implement.**

3a. `_open_bot_incident` (line 748): signature `..., *, fixable, novel=False, now=None` and pass `novel=novel` to `tracker.open(...)`.

3b. Imports: add `novel_recovery` to the `from watcherdog import (...)` block; add `format_novel_alert` to the alerter-names import (the block at `:44-50` with `format_incident_escalated` etc.).

3c. Delete the whole `_incident_via_agent` function (`:268-298`). Sole caller is the branch replaced below; `agent` stays imported (used by the ibo free-form path).

3d. Replace the final branch of `_evaluate_bot` (currently `:930-940`):

```python
    # Skill 2: when the agent can act and model calls are enabled, route the
    # incident through it. In DISABLE_AI mode, stay deterministic: scripted
    # fixes above may still run, but unresolved incidents become plain alerts.
    if (not cfg.disable_ai) and cfg.agent_actions_enabled and state.get("system_prompt"):
        ok = await _incident_via_agent(client, cfg, state, target, bot, severity, text, deliver)
    else:
        ok = await _alert(state, client, target, format_alert(bot, severity, analysis, text), deliver)
    store.record(bot, severity, analysis, h, text, notified=ok and deliver, ts=now)
    _open_bot_incident(state, bot, severity, analysis, text,
                       fixable=(fix_status == "failed"), now=now)
    log.info("ALERTED %s (%s, sent=%s)", bot, severity, ok and deliver)
```

with:

```python
    # Phase 4: a TRULY novel error (no learned mapping at all -> fix_status None)
    # gets the deterministic generic-restart ladder — the old _incident_via_agent
    # model path is gone in every mode. A KNOWN fix that failed (or an unposted
    # confirm card) keeps the plain alert: re-driving a different destructive
    # sequence on top of a learned fix would double-press the panel.
    if fix_status is None:
        recovery = await novel_recovery.attempt(client, cfg, bot, text,
                                                chat=ent, deliver=deliver)
        ok = await _alert(state, client, target,
                          format_novel_alert(bot, severity, analysis, text, recovery),
                          deliver)
        store.record(bot, severity, analysis, h, text, notified=ok and deliver, ts=now)
        ladder_ran = recovery.get("status") in ("attempted", "failed")
        _open_bot_incident(state, bot, severity, analysis, text,
                           fixable=ladder_ran, novel=True, now=now)
        tracker = state.get("tracker")
        if ladder_ran and tracker is not None:
            tracker.note_fix_attempt(f"bot_error:{bot}", "novel-ladder")
        log.info("ALERTED %s (%s, novel, recovery=%s, sent=%s)",
                 bot, severity, recovery.get("status"), ok and deliver)
        return
    ok = await _alert(state, client, target, format_alert(bot, severity, analysis, text), deliver)
    store.record(bot, severity, analysis, h, text, notified=ok and deliver, ts=now)
    _open_bot_incident(state, bot, severity, analysis, text,
                       fixable=(fix_status == "failed"), now=now)
    log.info("ALERTED %s (%s, sent=%s)", bot, severity, ok and deliver)
```

NOTE: `tracker` is re-fetched from state here; the earlier `tracker` local at `:865` is in scope but re-reading is harmless and explicit. `fix_status is None` covers: truly novel, AND actions-disabled/dry-run (try_auto_fix never ran) — in the latter case `attempt()`'s own gate returns `skipped` (no press) and the incident is still flagged novel. That's intended: a dry run flags novelty without pressing.

- [ ] **Step 4: Verify** — `python -c "import watcherdog.mcp_watcher"`; `grep -n "_incident_via_agent" watcherdog/` → EMPTY; the passthrough test passes; full green-check.
- [ ] **Step 5: Commit** — `git add watcherdog/mcp_watcher.py tests/test_novel_recovery.py && git commit -m "feat(monitor): novel-error ladder replaces _incident_via_agent (model path deleted)"`

---

## Task 6: Refix-tick routes `novel=1` rows to the ladder

**Files:** Modify `watcherdog/mcp_watcher.py` (`_incident_followup_tick`, the `did_refix` branch ~`:1258`); Test: append to `tests/test_novel_recovery.py`.

- [ ] **Step 1: Failing test** — real tracker, monkeypatched seams:

```python
def test_refix_tick_routes_novel_to_ladder(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird", fixable=True,
           novel=True, raw_excerpt="weird novel text", now=0.0)
    t.open("bot_error", "SF8", "bot_error:SF8", "high", "known", fixable=True,
           raw_excerpt="known text", now=0.0)
    calls = {"novel": [], "auto": []}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        calls["novel"].append(bot); return {"status": "attempted"}

    async def fake_autofix(client, cfg, bot, text, *, chat=None):
        calls["auto"].append(bot); return {"status": "failed"}

    async def fake_alert(*a, **k):
        return True

    monkeypatch.setattr(mcp_watcher.novel_recovery, "attempt", fake_attempt)
    monkeypatch.setattr(mcp_watcher.auto_fix, "try_auto_fix", fake_autofix)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher, "_entity_for", lambda state, bot: object())

    cfg = types.SimpleNamespace(incident_followup_interval=10,
                                incident_giveup_seconds=10_000,
                                incident_max_fix_retries=3)
    asyncio.run(mcp_watcher._incident_followup_tick(
        object(), cfg, t, "target", {}, now=1000.0, deliver=True))
    assert calls["novel"] == ["SF7"]
    assert calls["auto"] == ["SF8"]
    t.close()
```

(If `_incident_followup_tick`'s signature differs — check `grep -n "async def _incident_followup_tick" watcherdog/mcp_watcher.py` — match it; `now` may be positional.)

- [ ] **Step 2: Run, verify fail** — `calls["novel"] == []` (both routed to auto_fix).

- [ ] **Step 3: Implement** — in the `did_refix` branch, replace:

```python
            try:
                outcome = await auto_fix.try_auto_fix(
                    client, cfg, bot, row["raw_excerpt"] or row["summary"], chat=ent)
            except Exception:  # noqa: BLE001
                log.exception("incident re-fix raised for %s", bot)
                outcome = None
```

with:

```python
            err_text = row["raw_excerpt"] or row["summary"]
            try:
                if row.get("novel"):
                    # Phase 4: novel incidents re-run the generic ladder (no
                    # learned fix exists for auto_fix to find).
                    outcome = await novel_recovery.attempt(
                        client, cfg, bot, err_text, chat=ent, deliver=deliver)
                else:
                    outcome = await auto_fix.try_auto_fix(
                        client, cfg, bot, err_text, chat=ent)
            except Exception:  # noqa: BLE001
                log.exception("incident re-fix raised for %s", bot)
                outcome = None
```

(`row` is a plain dict from the planner — `.get` is safe; a legacy un-migrated row simply lacks the key → auto_fix path, the old behavior.)

- [ ] **Step 4: Run, verify pass** + full green-check.
- [ ] **Step 5: Mutation-verify** — flip `if row.get("novel")` to `if not row.get("novel")` → routing test FAILS both assertions; restore.
- [ ] **Step 6: Commit** — `git add watcherdog/mcp_watcher.py tests/test_novel_recovery.py && git commit -m "feat(monitor): refix loop re-runs the novel ladder within the existing retry budget"`

---

## Task 7: Holistic verification

**Files:** none (verification only).

- [ ] **Step 1:** Full green-check `pytest $(git ls-files 'tests/*.py') -q` — 0 failures (2 pre-existing skips).
- [ ] **Step 2:** `grep -rn "_incident_via_agent" watcherdog/ tests/` (tracked only) → empty. `grep -n "agent.answer" watcherdog/mcp_watcher.py` → only the ibo free-form / Special Forces paths (lines ~286 gone; remaining hits must be outside `_evaluate_bot`/`monitor_once` reachability).
- [ ] **Step 3:** Mutation spot-check end-to-end: revert Task 5's `fixable=ladder_ran` to `fixable=False` → the refix routing test still passes (it seeds rows directly), so verify instead via the passthrough test + planner: a `fixable=False` novel row gets `followup` not `refix` (assert via `incident_followup_step` with a real tracker if not already covered). Restore.
- [ ] **Step 4:** Trace read: `sed -n '880,960p' watcherdog/mcp_watcher.py` — confirm branch order (auto_fix outcomes → novel branch → known-fail alert) and that every path records to `store` exactly once.
- [ ] **Step 5:** PR per the repo flow (worktree → push → PR → reviewer pass → merge-if-clean).

---

## Self-Review (completed by plan author)

- **Spec coverage:** §A ladder+gates → Task 2; §B novel flag/migration/accessor → Task 1; §C wiring (final branch, refix routing, removal) → Tasks 5–6; §D config → Task 4; alerter line → Task 3; error handling (never-raise, critical-first, migration fallback, row["col"]) → embedded in Tasks 1/2/5; testing reqs incl. real tracker + mutation-verification → per-task steps.
- **Refinement encoded:** ladder fires only on `fix_status is None` (truly novel); known-fix-failed and unposted-confirm keep the plain-alert path (no double-driving). Spec updated to match.
- **Type consistency:** `attempt(client, cfg, bot, text, *, chat, deliver)` used identically in Tasks 2/5/6; outcome statuses (`human_needed/skipped/attempted/failed`) consistent across Tasks 2/3/5; `open(..., novel=False)` matches Tasks 1/5; `novel_list()` only in Task 1 (YAGNI — no UI consumer yet).
- **No placeholders:** every code step shows complete code; commands include expected outcomes. Two intentional execution-time checks are flagged inline (the `_fresh_config` helper name in Task 4 and `_incident_followup_tick`'s exact signature in Task 6) — both are read-and-mirror instructions, not gaps.
