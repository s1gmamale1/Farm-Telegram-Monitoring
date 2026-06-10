# Fix Campaign Phase C — Sweep Robustness, Dry-Run Isolation, Hourly Report — Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** One bad chat costs only itself (not the whole sweep); a dry run touches nothing
real (no Telegram sends, no ledger writes); and the hourly report delivers or disables
itself loudly instead of erroring 26×/day.

**Architecture:** Three independent fixes — exception isolation in `monitor_once`
(`mcp_watcher.py`), a `dry_run` flag on `IncidentTracker` + a one-arg `deliver` fix
(`incident_tracker.py` + `mcp_watcher.py`), and an hourly-target fallback
(`config.py` + `mcp_watcher.py`). Spec:
`docs/superpowers/specs/2026-06-10-deep-review-fix-campaign-design.md` (Phase C).
Phases A+B are merged (main `2be3315`); branch off current main.

**Tech Stack:** Python 3 / Telethon / pytest. No new dependencies.

**Environment (same discipline as A/B):**
- Worktree: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-phase-c -b fix/phase-c-robustness main`
- Tests use the main venv: `cd /tmp/wd-phase-c && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`. NEVER bare `pytest`.
- Authoritative suite: `pytest $(git ls-files 'tests/*.py')`. Baseline on main: green.
- Merge: `git push` FIRST, then `gh pr merge --rebase`, then grep main for a marker.
- READ the target test file before adding tests; reuse existing helpers (`_cfg`, `_FakeClient`,
  real `IncidentTracker`/`IncidentStore`).

**Key facts (verified against main `2be3315`):**
- `monitor_once` per-chat loop (`mcp_watcher.py:961`): `latest_message` is wrapped (968-973);
  `_evaluate_panel` is wrapped (978-984); the `_evaluate_bot` call (988-989) and the whole
  silence block (991-1031) + the `healthy += 1` (1033-1034) are NOT wrapped → an exception in
  either aborts the rest of the sweep.
- The bare `try_auto_fix` inside `_evaluate_bot` (887) is covered once the `_evaluate_bot` CALL
  is wrapped.
- The ibo agent-answer send `mcp_watcher.py:1143` is `await _send(client, target, answer,
  cfg=cfg, sticker_ok=True)` — MISSING the `deliver` positional (compare line 1124 which has
  it; `_send` signature is `_send(client, target, text, deliver=True, *, cfg=None, sticker_ok=False)`).
- `IncidentTracker.__init__(self, db_path)` (`incident_tracker.py:20`). Mutating methods:
  `open` (78), `resolve_by_bot` (100), `resolve_open_for_bot` (126/152), `note_fix_attempt`,
  `mark_followed_up`, `escalate`, `refresh` (100-124), and `_by_id` variants
  `note_fix_attempt_by_id`/`mark_followed_up_by_id`/`escalate_by_id` (~214-246). Query methods
  (`open_for_bot`, `open_list`, `open_list_for_bot`, `due_for_followup`, `due_for_giveup`,
  `get_open_by_id`) must stay live in dry-run.
- The tracker is created at startup in `run()` (signature has `deliver=True`, line 1635):
  `mcp_watcher.py:1660` `state["tracker"] = IncidentTracker(cfg.db_path)` — `deliver` IS in scope.
- `config.py`: `self.ibo_chat_id` (allow-list primary) is set at line 149; `self.telegram_chat_id`
  at 66; the hourly fallback at line 223 `self.hourly_report_chat = raw_hourly_chat if
  raw_hourly_chat else self.telegram_chat_id`. Production log: 26×/day
  `Cannot find any entity corresponding to ""` → both HOURLY_REPORT_CHAT and TELEGRAM_CHAT_ID
  were empty; `ibo_chat_id` IS set (alerts deliver).
- `run_hourly_report` (1418): resolves `chat_ref = cfg.hourly_report_chat` (~1490), coerces a
  numeric string to int, then `await client.get_entity(chat_ref)` (errors on `""`).

---

### Task 1: sweep isolates a failing chat (exception per chat, not per sweep)

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — `monitor_once` per-chat body (988-1034)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test.** A `monitor_once` over two chats where the FIRST
  raises inside `_evaluate_bot` must still process the SECOND. Reuse `_cfg`/`_FakeClient`.
  Monkeypatch `mcp_watcher._evaluate_bot` to raise for one bot and record the other; provide a
  minimal `watch=[("Bad", ent1), ("Good", ent2)]`, `latest_message` returning a benign text, and
  `panel_rules_enabled=false` so the panel path is skipped. Assert the "Good" bot was still
  evaluated (the recorder saw it) and `monitor_once` did not raise.

```python
def test_sweep_continues_after_one_chat_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, {"PANEL_RULES_ENABLED": "false", "SILENCE_ENABLED": "false"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    seen = []

    async def fake_latest(client, ent, mark_read=False):
        return "some text", None

    async def fake_eval(client, cfg, store, state, target, bot, text, now, loop,
                        deliver=True, ent=None, date=None):
        if bot == "Bad":
            raise RuntimeError("boom in Bad")
        seen.append(bot)

    monkeypatch.setattr(mcp_watcher.tg_tools, "latest_message", fake_latest)
    monkeypatch.setattr(mcp_watcher, "_evaluate_bot", fake_eval)

    watch = [("Bad", object()), ("Good", object())]
    asyncio.run(mcp_watcher.monitor_once(
        client, cfg, store, {}, watch, "ibo", deliver=True))
    assert seen == ["Good"]   # the failing chat didn't abort the sweep
```
Confirm the existing `_cfg` accepts `PANEL_RULES_ENABLED`/`SILENCE_ENABLED` keys (they map to
`panel_rules_enabled`/`silence_enabled`); if the names differ, set them via the real env keys.

- [ ] **Step 2:** Run, confirm it FAILS (the RuntimeError propagates out of `monitor_once`).
- [ ] **Step 3: Implement.** Wrap the `_evaluate_bot` call + silence block in one per-chat
  try/except that mirrors the `_evaluate_panel` pattern. Replace lines 988-1034 so the body is:
  ```python
        try:
            await _evaluate_bot(client, cfg, store, state, target, name, text, now, loop,
                                deliver, ent=ent, date=date)

            if cfg.silence_enabled:
                ... (the entire existing silence block, unchanged, indented one level) ...

            if not state.get(name + "::err") and not state.get(name + "::silent"):
                healthy += 1
        except Exception:  # noqa: BLE001
            log.exception("bot/silence eval failed for %s; continuing", name)
            continue
  ```
  IMPORTANT: indent the existing silence block + the `healthy` check into the `try`. Do NOT
  change their logic. The `continue` skips to the next chat (the `healthy` count is inside the
  try, so a failed chat isn't counted healthy).
- [ ] **Step 4:** Run `tests/test_mcp_watcher_core.py` — all pass.
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(monitor): isolate per-chat eval so one bad chat can't abort the sweep`.

---

### Task 2: dry-run touches nothing real (agent-answer send + tracker ledger)

**Files:**
- Modify: `watcherdog/mcp_watcher.py:1143` (the `deliver` one-arg fix) and `:1660` (tracker
  creation passes `dry_run`)
- Modify: `watcherdog/incident_tracker.py` — `__init__` gains `dry_run`; mutators no-op when set
- Test: `tests/test_incident_tracker.py`, `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write failing tests.**
  (a) `tests/test_incident_tracker.py` — `IncidentTracker(path, dry_run=True)`: call EVERY mutator
  (`open`, `resolve_by_bot`, `resolve_open_for_bot`, `refresh`, `note_fix_attempt`,
  `mark_followed_up`, `escalate`, `note_fix_attempt_by_id`, `mark_followed_up_by_id`,
  `escalate_by_id`); assert `open_list()` stays `[]` (nothing written) and no exception. Then a
  POSITIVE control: a `dry_run=False` tracker's `open(...)` DOES write (`open_list()` len 1) —
  proving the test would catch a guard that's always-on.
  ```python
  def test_dry_run_tracker_writes_nothing(tmp_path):
      t = IncidentTracker(str(tmp_path / "dry.db"), dry_run=True)
      t.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom", fixable=False, now=1.0)
      t.resolve_by_bot("bot_error", "Bot1", "self_healed", now=2.0)
      t.refresh("bot_error:Bot1", "critical", "x")
      t.note_fix_attempt("bot_error:Bot1", "retry")
      t.mark_followed_up("bot_error:Bot1", now=3.0)
      t.escalate("bot_error:Bot1", now=4.0)
      t.note_fix_attempt_by_id(1, "retry")
      t.mark_followed_up_by_id(1, now=5.0)
      t.escalate_by_id(1, now=6.0)
      assert t.open_list() == []
      t.close()

  def test_live_tracker_still_writes(tmp_path):
      t = IncidentTracker(str(tmp_path / "live.db"))   # dry_run defaults False
      t.open("bot_error", "Bot1", "bot_error:Bot1", "high", "boom", fixable=False, now=1.0)
      assert len(t.open_list()) == 1
      t.close()
  ```
  (b) `tests/test_mcp_watcher_core.py` — the agent-answer send honors `deliver`: this is the ibo
  listener path; if a focused test is hard, the one-line change is covered by inspection + the
  existing `test_send_dry_run_does_not_call_send_message`. Add a targeted check only if the ibo
  listener already has a test harness (search `register_ibo_listener` in the test file); else
  note it's an inspection-verified one-liner consistent with line 1124.
- [ ] **Step 2:** Run, confirm (a) FAILS (`__init__` has no `dry_run` kwarg → TypeError).
- [ ] **Step 3: Implement.**
  - `incident_tracker.py` `__init__`: add `*, dry_run=False`, store `self._dry_run = dry_run`.
    Add a one-line guard `if self._dry_run: return None` (or `return` for void methods) at the TOP
    of each of the 10 mutators (`open`, `resolve_by_bot`, `resolve_open_for_bot`, `refresh`,
    `note_fix_attempt`, `mark_followed_up`, `escalate`, `note_fix_attempt_by_id`,
    `mark_followed_up_by_id`, `escalate_by_id`). Query methods unchanged. Add a class docstring
    line documenting the dry-run contract.
  - `mcp_watcher.py:1143`: `ok = await _send(client, target, answer, deliver, cfg=cfg, sticker_ok=True)`.
  - `mcp_watcher.py:1660`: `state["tracker"] = IncidentTracker(cfg.db_path, dry_run=not deliver)`.
- [ ] **Step 4:** Run `tests/test_incident_tracker.py tests/test_mcp_watcher_core.py` — all pass
  (the existing key-based + by-id mutator tests use `dry_run=False` default → still write → green).
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(monitor): dry-run isolation — agent-answer honors deliver, tracker writes nothing`.

---

### Task 3: hourly report falls back to the allow-list primary (or disables loudly)

**Files:**
- Modify: `watcherdog/config.py:223` (fallback chain)
- Modify: `watcherdog/mcp_watcher.py` — `run_hourly_report` empty-target guard (~1490)
- Test: `tests/test_config.py`, `tests/test_hourly_report.py` (or `tests/test_mcp_watcher_core.py`)

- [ ] **Step 1: Write failing tests.**
  (a) `tests/test_config.py` — with `HOURLY_REPORT_CHAT` and `TELEGRAM_CHAT_ID` unset but
  `IBO_CHAT_ID`/`ALLOWLIST` set, `cfg.hourly_report_chat` equals the allow-list primary
  (`cfg.ibo_chat_id`). Reuse the file's `clean_environ` autouse fixture + `Config({...})` pattern.
  ```python
  def test_hourly_report_chat_falls_back_to_allowlist_primary():
      cfg = Config({"ALLOWLIST": "1406109190, @second"})   # no HOURLY_REPORT_CHAT, no TELEGRAM_CHAT_ID
      assert cfg.hourly_report_chat == "1406109190"
      assert cfg.hourly_report_chat == cfg.ibo_chat_id

  def test_hourly_report_chat_prefers_explicit_over_fallback():
      cfg = Config({"HOURLY_REPORT_CHAT": "-100999", "ALLOWLIST": "111"})
      assert cfg.hourly_report_chat == "-100999"
  ```
  (b) `run_hourly_report` empty-target guard: with `cfg.hourly_report_chat == ""` (force it),
  `run_hourly_report` returns False WITHOUT calling `client.get_entity` (a `_FakeClient` whose
  `get_entity` appends to a list → assert empty). Place in `tests/test_mcp_watcher_core.py` or
  `tests/test_hourly_report.py` (whichever already has a client fake for this path).
- [ ] **Step 2:** Run, confirm (a) FAILS (today `hourly_report_chat` is `""` when both are unset).
- [ ] **Step 3: Implement.**
  - `config.py:223`: `self.hourly_report_chat = raw_hourly_chat or self.telegram_chat_id or self.ibo_chat_id`
    (chain to the allow-list primary; `ibo_chat_id` is defined above at line 149).
  - `run_hourly_report` (after `chat_ref = cfg.hourly_report_chat`, ~1490): guard
    ```python
        chat_ref = cfg.hourly_report_chat
        if not chat_ref:
            log.warning("hourly report: no target chat configured (set HOURLY_REPORT_CHAT "
                        "or ALLOWLIST/IBO_CHAT_ID) — skipping")
            return False
    ```
    so a truly-unconfigured deploy logs ONE line per call instead of an exception, and never calls
    `get_entity("")`. (With the config fallback, a normal deploy now has a real target and never
    hits this guard.)
- [ ] **Step 4:** Run `tests/test_config.py tests/test_hourly_report.py` — all pass.
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(monitor): hourly report falls back to the allow-list primary, skips cleanly when unset`.

---

### Task 4: full-suite gate, PR, holistic review, merge

- [ ] **Step 1:** `pytest $(git ls-files 'tests/*.py')` — 0 failures.
- [ ] **Step 2:** push `fix/phase-c-robustness`; open PR.
- [ ] **Step 3:** holistic review across the 3 commits — focus: does the Task 1 try/except
  swallow an exception that SHOULD propagate (e.g. a `KeyboardInterrupt`/cancellation)? (Use
  `except Exception` — already excludes `BaseException`/`CancelledError`; confirm.) Does the
  Task 2 dry-run flag leave any mutator unguarded (audit all 10)? Does the Task 3 config fallback
  interact with `validate()` (an empty hourly target is no longer possible when ibo is set)?
  Fix findings; re-run suite.
- [ ] **Step 4: Merge (push-first):** `git push` → `gh pr merge --rebase` → checkout+pull main →
  grep main for markers (`bot/silence eval failed`, `self._dry_run`, `or self.ibo_chat_id`).

---

## Self-review notes
- Tasks are fully independent (different functions/files) — order doesn't matter, but Task 2's
  dry-run flag and Task 1's isolation both touch `monitor_once`/startup, so implement sequentially
  to avoid line-number drift.
- Tiering: Task 1 (exception isolation) and Task 3 (config fallback) are mechanical → implementer
  + lead mutation-verification. Task 2 (10 mutator guards — easy to miss one) gets a full review.
- Reproduced/observed in production: Task 3 (the 26×/day error is in the log). Task 1 and the
  dry-run leaks are code-traced.
- Watch-out: Task 1's `except Exception` must NOT catch `asyncio.CancelledError` (it doesn't —
  that's `BaseException` in 3.8+). Confirm the Python version (3.14 here) keeps `CancelledError`
  outside `Exception`. It does.
