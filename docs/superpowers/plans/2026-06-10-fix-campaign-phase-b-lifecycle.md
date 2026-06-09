# Fix Campaign Phase B — Make the Incident Lifecycle Truthful — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Every ✅/⏳/❌ incident message reflects reality — resolves prove *health* not
*traffic*, new/escalating errors are never hidden behind an open incident, restarts don't
orphan rows into false "needs PC" escalations, and the followup refix actually presses
panel buttons.

**Architecture:** Eight surgical fixes across `watcherdog/mcp_watcher.py` (the `_evaluate_bot`
classify→suppress→alert path, the silence-recovery branch, the followup tick, startup
re-arm), `watcherdog/storage.py` (`last_seen`), and read-only use of existing
`watcherdog/incident_tracker.py` methods (`resolve_by_bot`, `open_list`, `open_for_bot`).
Spec: `docs/superpowers/specs/2026-06-10-deep-review-fix-campaign-design.md` (Phase B).
Phase A is merged (main `d77cdf7`); branch this off current main.

**Tech Stack:** Python 3 / Telethon / pytest. No new dependencies.

**Environment (read first — same discipline as Phase A):**
- Worktree: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-phase-b -b fix/phase-b-lifecycle main`
- Tests use the main checkout's venv: `cd /tmp/wd-phase-b && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`. NEVER bare `pytest`.
- Authoritative suite: `pytest $(git ls-files 'tests/*.py')`. Baseline on main: green.
- Merge: `git push` FIRST, then `gh pr merge --rebase`, then grep main for a marker line.
- **READ the target test file before adding tests** (Phase A hit a pre-existing-constant
  collision). The lifecycle tests live in `tests/test_mcp_watcher_core.py` (65 tests) and
  `tests/test_incident_tracker.py` (16). Check for existing fakes/constants and reuse them.

**Key facts (verified against main):**
- `_evaluate_bot(client, cfg, store, state, target, bot, text, now, loop, deliver=True, ent=None)`
  — receives `text` + `now` but NOT the message date. Caller `monitor_once` (line ~925) HAS
  `date` in scope (from `latest_message` at ~906).
- `incident_tracker` already exposes: `open_for_bot(source, bot)` → open row dict or None;
  `resolve_by_bot(source, bot, resolution, now=None)` (scoped, currently UNUSED in prod);
  `open_list()` → all open rows; the open row dict includes `opened_ts`, `severity`, `key`,
  `raw_excerpt`, `fix_retries`.
- The classify-normal resolve is at `_evaluate_bot` line ~782; the dedupe early-return at
  ~818-821; the bot_error suppression gate at ~829-834; the silence-recovery branch at
  `monitor_once` ~958-965; the startup tracker creation at line ~1578
  (`state["tracker"] = IncidentTracker(cfg.db_path)`); the followup tick refix at ~1108-1125.
- `error_hash`/`classify` imported at top of `mcp_watcher.py`.

Task order is chosen so signature changes land before the call sites that depend on them.

---

### Task 1: `_evaluate_bot` takes the message date; classify-normal resolve is freshness-gated

A silent bot's latest message is often a routine drop line that classifies "normal". Every
sweep re-runs `_evaluate_bot` on that stale message → it resolves the bot's just-opened
incident with a false "✅ recovered on its own" while the bot is still dark. Gate the
resolve so it only fires when the proving message is **newer than the incident's `opened_ts`**.

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — `_evaluate_bot` signature + the `bucket == "normal"` branch; the `monitor_once` call site
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** (read the file first; reuse any existing tracker fake / `_cfg` helper). The test drives `_evaluate_bot` directly with a fake tracker whose open row has a recent `opened_ts`, and a "normal" message whose date is OLDER than that:

```python
def test_stale_normal_message_does_not_resolve_incident(monkeypatch):
    # A silent bot's last message is an old drop line (classifies "normal"). It must
    # NOT close an incident opened AFTER that message — health needs a fresh proof.
    import time as _t
    resolved = []

    class _FakeTracker:
        def open_for_bot(self, source, bot):
            # an open bot_error opened 10s ago
            return {"key": f"bot_error:{bot}", "opened_ts": _t.time() - 10,
                    "severity": "high", "raw_excerpt": "boom"}
        def resolve_open_for_bot(self, bot, resolution, now=None):
            resolved.append(bot); return {"count": 1, "elapsed": 1.0, "we_fixed": False}

    state = {"tracker": _FakeTracker()}
    cfg = _make_cfg(disable_ai=True)            # use the file's existing cfg helper
    store = _make_store()                        # existing in-memory store helper
    old_date = type("D", (), {"timestamp": staticmethod(lambda: _t.time() - 600)})()
    import asyncio
    asyncio.run(mcp_watcher._evaluate_bot(
        None, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$", _t.time(),
        _Loop(), deliver=True, ent=None, date=old_date))
    assert resolved == []        # stale normal msg must not resolve


def test_fresh_normal_message_resolves_incident(monkeypatch):
    import time as _t
    resolved = []

    class _FakeTracker:
        def open_for_bot(self, source, bot):
            return {"key": f"bot_error:{bot}", "opened_ts": _t.time() - 600,
                    "severity": "high", "raw_excerpt": "boom"}
        def resolve_open_for_bot(self, bot, resolution, now=None):
            resolved.append(bot); return {"count": 1, "elapsed": 600.0, "we_fixed": False}

    state = {"tracker": _FakeTracker()}
    cfg = _make_cfg(disable_ai=True)
    store = _make_store()
    fresh_date = type("D", (), {"timestamp": staticmethod(lambda: _t.time())})()
    import asyncio
    asyncio.run(mcp_watcher._evaluate_bot(
        None, cfg, store, state, "ibo", "Bot1",
        "🎁 collected drop · AK-47 - 0.27$", _t.time(),
        _Loop(), deliver=True, ent=None, date=fresh_date))
    assert resolved == ["Bot1"]  # message newer than opened_ts -> real recovery
```

NOTE: `_make_cfg`, `_make_store`, `_Loop` are placeholders — **use whatever the existing
`tests/test_mcp_watcher_core.py` already provides** for building a cfg, an in-memory
`IncidentStore`, and a fake `loop` with `run_in_executor`. If the file resolves "normal"
through a different entry helper than calling `_evaluate_bot` directly, mirror that file's
established pattern instead. The two behaviors to pin are the only contract: stale normal →
no resolve; fresh normal → resolve.

- [ ] **Step 2:** Run the two tests, confirm the stale one FAILS (resolve fires today).
- [ ] **Step 3: Implement.**
  - Add `date=None` to the `_evaluate_bot` signature (after `ent=None`).
  - Replace the `bucket == "normal"` branch so the resolve is freshness-gated:
    ```python
    if bucket == "normal":
        tracker = state.get("tracker")
        row = tracker.open_for_bot("bot_error", bot) if tracker is not None else None
        # Only a message NEWER than the incident proves health; a stale routine
        # line re-read every sweep must not close a still-open error.
        fresh = (row is None or date is None
                 or date.timestamp() >= row.get("opened_ts", 0))
        if fresh:
            await _resolve_incidents_for(state, client, target, bot, now, deliver, cfg)
        state[bot + "::err"] = False
        return
    ```
  - At the `monitor_once` call site (line ~925) pass `date=date`:
    ```python
    await _evaluate_bot(client, cfg, store, state, target, name, text, now, loop,
                        deliver, ent=ent, date=date)
    ```
- [ ] **Step 4:** Run `tests/test_mcp_watcher_core.py` — all pass.
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(monitor): freshness-gate the classify-normal incident resolve`.

---

### Task 2: silence-recovery closes only the `silence` source, not every incident

`monitor_once`'s silence-recovery branch (`elif not silent and was:`) calls
`_resolve_incidents_for`, which closes EVERY open incident for the bot (any source). One
sweep can OPEN a `bot_error` (the error IS the fresh traffic that ends silence) and then
the silence branch closes it with "back online". Use the scoped, already-existing
`resolve_by_bot("silence", …)`.

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — the `elif not silent and was:` branch (~958-965)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** — drive `monitor_once` (or the silence-recovery
  path the file already exercises) such that a bot transitions silent→posting where the new
  message is an ERROR; assert the open `bot_error` incident is NOT resolved, only the
  `silence` one is. Reuse the file's existing `monitor_once` harness/fakes. Minimal contract:
  a fake tracker records which `(method, source)` resolve calls happen; assert
  `resolve_by_bot("silence", name, ...)` is used and `resolve_open_for_bot`/cross-source is
  NOT called from this branch.

```python
def test_silence_recovery_resolves_only_silence_source(monkeypatch):
    calls = []

    class _FakeTracker:
        def open(self, *a, **k): pass
        def open_for_bot(self, source, bot): return None
        def resolve_by_bot(self, source, bot, resolution, now=None):
            calls.append(("by_bot", source, bot)); return {"count": 1, "elapsed": 5.0, "we_fixed": False}
        def resolve_open_for_bot(self, bot, resolution, now=None):
            calls.append(("open_for_bot", bot)); return None
    # ... build state so name was silent (state[name+"::silent"]=True) and now posts;
    # run the silence-recovery branch; assert:
    assert ("by_bot", "silence", name) in calls
    assert all(c[0] != "open_for_bot" for c in calls)
```
Adapt construction to the file's existing silence-path test (there is already silence
coverage in `test_mcp_watcher_core.py` — model the new test on it).

- [ ] **Step 2:** Run, confirm it FAILS (today the branch calls `_resolve_incidents_for` →
  `resolve_open_for_bot`).
- [ ] **Step 3: Implement** — replace the resolve call in the `elif not silent and was:`
  branch. `format_recovery_alert` already announced "back online", so resolve the silence
  source SILENTLY (no second ✅):
  ```python
            elif not silent and was:
                await _alert(state, client, target, format_recovery_alert(name), deliver)
                state[key] = False
                # Scope the closure to the SILENCE source only: a bot_error that
                # arrived as the silence-ending traffic must stay open. recovery
                # alert already announced, so close silently.
                tracker = state.get("tracker")
                if tracker is not None:
                    tracker.resolve_by_bot("silence", name, "self_healed", now=now)
                log.info("RECOVERED: %s", name)
  ```
- [ ] **Step 4-6:** panel+core suites pass; full suite 0 failures; commit
  `fix(monitor): silence-recovery closes only the silence incident, not bot_errors`.

---

### Task 3: suppression gate alerts on a genuinely NEW or higher-severity error

The bot_error suppression gate (~829-834) keys only on `(bot_error, bot)`: an open MEDIUM
silently swallows a later, DIFFERENT CRITICAL (e.g. "account banned") on every channel and
skips auto-fix for it. Alert + refresh the open row when the new hash differs OR severity
rose; otherwise suppress as today.

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — the suppression gate (~829-834); `_open_bot_incident`
  must store `raw_hash` so the gate can compare (check `incident_tracker.open` signature —
  it already accepts `raw_excerpt`; add hash storage via the existing row or compare on
  `raw_excerpt`’s `error_hash`).
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write failing tests** — (a) open HIGH incident, feed a DIFFERENT-hash
  critical message → assert an alert IS sent (not swallowed); (b) open HIGH, feed the SAME
  message again → assert still suppressed (no new alert). Use `SEVERITY_ORDER` (already in
  `mcp_watcher.py`).

```python
def test_open_incident_does_not_hide_new_critical(monkeypatch):
    # open HIGH bot_error; a different-hash CRITICAL arrives -> must alert, not suppress.
    ...
    assert alerts, "a new distinct/higher error must not be swallowed by an open incident"

def test_open_incident_still_suppresses_same_symptom(monkeypatch):
    # same hash, same severity, incident already open -> suppressed (no new alert).
    ...
    assert alerts == []
```
Model construction on the existing `test_open_incident_suppresses_duplicate_detection_alert`
test already in the file (find it; this extends its scenario).

- [ ] **Step 2:** Run, confirm (a) FAILS (currently swallowed).
- [ ] **Step 3: Implement** — change the gate to compare. The open row dict carries
  `severity` and `raw_excerpt`; compute `error_hash(row["raw_excerpt"])` vs the new `h`:
  ```python
    tracker = state.get("tracker")
    open_row = tracker.open_for_bot("bot_error", bot) if tracker is not None else None
    if open_row is not None:
        same_hash = error_hash(open_row.get("raw_excerpt") or "") == h
        not_worse = SEVERITY_ORDER.get(severity, 2) <= SEVERITY_ORDER.get(open_row.get("severity"), 2)
        if same_hash and not_worse:
            log.info("bot_error incident already open for %s — suppressing duplicate alert", bot)
            store.record(bot, severity, analysis, h, text, notified=False, ts=now)
            return
        # else: a DIFFERENT or HIGHER-severity error — fall through to alert + refresh
        log.info("new/worse error for %s while incident open — alerting", bot)
  ```
  Then ensure the fall-through path (alert + `_open_bot_incident`) refreshes the open row.
  `incident_tracker.open` is idempotent per key `bot_error:{bot}` — confirm it UPDATES
  severity/summary/raw_excerpt on re-open of an existing open row; if it does NOT (it may
  no-op when a row is already open), add a small `tracker.refresh(key, severity, summary,
  raw_excerpt)` method OR widen `open` to update an existing open row's
  severity/summary/excerpt. Pick the minimal change; TEST that after the new critical the
  open row's severity is "critical".

  NOTE: this task may need an `incident_tracker.py` method addition — that's the one place in
  Phase B touching the tracker. Keep it minimal and unit-test it in
  `tests/test_incident_tracker.py`.

- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): suppression gate alerts on new/higher-severity errors, refreshes the row`.

---

### Task 4: re-open (silently) inside the dedupe window so a recurrence isn't untracked

After any resolve, an identical error recurring within `DEDUPE_WINDOW` (default 300s) hits
the `last_seen` early-return (~818-821) and returns BEFORE `_open_bot_incident` — so the bot
is broken again with no open incident, no followups, no alert. Re-open the ledger row
(idempotent) before that return; keep the no-spam property (no new alert).

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — the dedupe early-return (~818-821)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** — first sweep alerts+opens; resolve; second
  identical error within the dedupe window → assert NO new alert (dedupe holds) BUT the
  incident is re-opened (tracker has an open row again). Use a fake tracker that records
  `open(...)` calls.
- [ ] **Step 2:** Run, confirm it FAILS (no re-open today).
- [ ] **Step 3: Implement** — in the dedupe branch, re-open before returning:
  ```python
    last = store.last_seen(h)
    if last is not None and (now - last) < cfg.dedupe_window:
        log.info("error on %s already alerted %.0fs ago; not resending", bot, now - last)
        # Still track it: a recurrence within the dedupe window must not leave the
        # bot with no open incident (open is idempotent per bot — no alert spam).
        _open_bot_incident(state, bot, severity, analysis, text,
                           fixable=_is_fixable(analysis, text), now=now)
        return
  ```
  Use the SAME `fixable` derivation the alert path uses below (extract it to a local/helper
  if it's inline, so both call sites agree — DRY).
- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): re-open the incident on a dedupe-window recurrence (no alert spam)`.

---

### Task 5: startup re-arms panel episodes from the durable ledger

Panel episode latches (`_PANEL_STATE`) are process-memory; open `panel:` rows persist in
SQLite. After a restart, a panel that healed during downtime has no latch → the followup
loop nags and (since `opened_ts` predates the restart) escalates "❌ needs PC" for a healthy
panel on the first tick. At startup, re-arm `coldcase_reported` from open `panel:` rows so
the FSM owns them (the next healthy sweep then resolves via Phase A's ledger-aware closure).

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — right after `state["tracker"] = IncidentTracker(cfg.db_path)` (~1578)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** — call the re-arm helper (extract the re-arm into a
  small `_rearm_panel_episodes(state)` function so it's unit-testable) with a fake tracker
  whose `open_list()` returns one `source="panel"` row for "SinFermera2" and one
  `source="bot_error"` row; assert `_PANEL_STATE["SinFermera2"].coldcase_reported is True`
  and the bot_error one created no panel state.
- [ ] **Step 2:** Run, confirm FAILS (no such function / no re-arm).
- [ ] **Step 3: Implement** — add the helper and call it at startup:
  ```python
  def _rearm_panel_episodes(state):
      """After a (re)start, in-memory panel latches are empty but open panel: rows
      persist. Re-arm coldcase_reported from the ledger so the followup loop doesn't
      falsely escalate, and the next healthy sweep resolves the row."""
      tracker = state.get("tracker")
      if tracker is None:
          return
      for row in tracker.open_list():
          if row.get("source") == "panel":
              ps = _PANEL_STATE.setdefault(row["bot"], panel_rules.PanelState())
              ps.coldcase_reported = True
  ```
  Call `_rearm_panel_episodes(state)` immediately after the tracker is created (~1578).
- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): re-arm panel episodes from the ledger at startup (no false escalations)`.

---

### Task 6: per-bot last-evaluated-message memo + `notified=1` dedupe filter

`_evaluate_bot` re-analyzes/re-records the bot's UNCHANGED latest message every sweep:
below-threshold rows pile up → false "🔁 recurring 30×", and `last_seen` returns the newest
ts regardless of `notified`, so undeduped low rows keep a hash "fresh" and can suppress a
later REAL alert forever; plus a wasted Ollama call/sweep. Skip unchanged messages, and make
the dedupe lookup consider only NOTIFIED rows.

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — top of `_evaluate_bot`; `watcherdog/storage.py` — `last_seen`
- Test: `tests/test_mcp_watcher_core.py`, `tests/test_storage.py`

- [ ] **Step 1: Write failing tests** — (a) storage: `last_seen(h)` ignores `notified=0`
  rows when asked for the dedupe view (add `last_seen(h, notified_only=True)` or a new
  `last_notified(h)`); (b) memo: feeding `_evaluate_bot` the SAME (bot, message) twice in a
  row runs analysis/record only ONCE.
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3: Implement.**
  - `storage.last_seen`: add `notified_only=False`; when True, `WHERE raw_hash=? AND notified=1`.
  - `_evaluate_bot`: near the top, memo on `(bot, error_hash(text))` (or message id if the
    sweep has it — it does not pass id, so hash is the key) in `state`:
    ```python
    memo_key = bot + "::last_eval_hash"
    h_pre = error_hash(text) if text else ""
    if text and state.get(memo_key) == h_pre:
        return                      # unchanged latest message — already handled
    state[memo_key] = h_pre
    ```
    Place AFTER the empty-text guard but BEFORE classify/analysis. CAUTION: do not memo away
    the "normal" resolve path incorrectly — a healthy message should still allow resolve on
    the FIRST time it's seen; the memo prevents only re-processing the IDENTICAL message
    again, which is correct (resolve already happened on first sight). Verify the Task 1
    freshness tests still pass.
  - Use `notified_only=True` at the dedupe gate's `last_seen` call.
- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): memo unchanged messages + notified-only dedupe (no false recurring/suppression)`.

---

### Task 7: followup refix resolves the real panel entity (and never DMs a stranger)

The followup tick refix (~1108-1125) calls `try_auto_fix(client, cfg, bot, row["raw_excerpt"])`
WITHOUT `chat=`, so `try_auto_fix` resolves the bot's DISPLAY NAME as a Telegram username →
guaranteed exception (swallowed) → the retry budget is burned with zero button presses while
the owner reads "retrying the fix…", and a colliding public username could be DM'd. Resolve
the entity from `state["watch"]` and pass it; skip the refix when the name isn't in the roster.

**Files:**
- Modify: `watcherdog/mcp_watcher.py` — `_incident_followup_tick` refix call (~1114-1115)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** — fake `try_auto_fix` capturing its `chat` kwarg;
  a followup tick with a refix action for a bot present in `state["watch"]` must call
  `try_auto_fix` with `chat=<entity>` (not None); a refix for a bot NOT in the roster must
  SKIP the press (and not burn a retry). Inspect how `state["watch"]` maps name→entity
  (check `monitor_once`/`load_watch_chats` — `watch` is a list of (name, ent) or a dict;
  confirm the shape and resolve accordingly).
- [ ] **Step 2:** Run, confirm FAILS (chat is None today).
- [ ] **Step 3: Implement** — look up the entity from the roster and pass `chat=`:
  ```python
    ent = _entity_for(state, bot)   # find ent by name in state["watch"]; None if absent
    if ent is None:
        log.info("refix skipped for %s — not in watch roster", bot)
        # plain followup only; do not burn a retry on an unreachable target
    else:
        outcome = await auto_fix.try_auto_fix(
            client, cfg, bot, row["raw_excerpt"] or row["summary"], chat=ent)
  ```
  Add a small `_entity_for(state, bot)` helper matching the actual `state["watch"]` shape.
  Confirm `try_auto_fix`'s signature accepts `chat=` (it does — the detection-time call at
  ~857 passes `chat=ent`).
- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): followup refix presses the real panel entity, skips off-roster bots`.

---

### Task 8: followup tick mutates the incident by row id, not by key (no stale-snapshot writes)

`incident_followup_step` snapshots rows, then for each action the tick awaits network/button
presses (tens of seconds) before `escalate`/`note_fix_attempt`/`mark_followed_up` — all keyed
by `key` ("most recent open row for key"). If a sweep resolves+reopens during the await, the
keyed mutation lands on the NEW row (budget pre-burned, `last_update_ts < opened_ts`); or a
`⏳/❌` is sent after the owner already got ✅. Re-fetch by row id and skip if gone.

**Files:**
- Modify: `watcherdog/incident_tracker.py` — add id-keyed variants (or an `id` param) to
  `escalate`/`note_fix_attempt`/`mark_followed_up`, and a `get_open_by_id(id)`; `mcp_watcher.py`
  — `_incident_followup_tick` re-fetches by id before each action.
- Test: `tests/test_incident_tracker.py`, `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write failing tests** — (a) tracker: `escalate_by_id`/`note_fix_attempt_by_id`
  no-op when the row id is no longer open; (b) tick: simulate resolve+reopen mid-await,
  assert the NEW row's `fix_retries`/`update_count` are untouched and no ⏳/❌ is sent for the
  resolved row.
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3: Implement** — the followup snapshot rows carry `id`. Add id-keyed methods
  (or `*_by_id`) to the tracker that `WHERE id=? AND status='open'`; have
  `incident_followup_step`/`_incident_followup_tick` carry the row `id` and, before executing
  each action, re-fetch `tracker.get_open_by_id(row["id"])`; if None, skip the action.
  Mutate via the id-keyed methods. Keep `incident_followup_step` pure (it already is — pass
  ids through the action dicts).
- [ ] **Step 4-6:** suites pass; full suite 0 failures; commit
  `fix(monitor): followup tick mutates incidents by row id, skips resolved rows`.

---

### Task 9: full-suite gate, PR, holistic review, merge

- [ ] **Step 1:** `pytest $(git ls-files 'tests/*.py')` — 0 failures.
- [ ] **Step 2:** push `fix/phase-b-lifecycle`; open PR.
- [ ] **Step 3:** holistic review across all 8 commits (cross-commit interactions —
  especially Task 1's freshness gate ↔ Task 6's memo, and Task 3's row-refresh ↔ Task 8's
  id-keyed mutation). Fix findings; re-run suite.
- [ ] **Step 4: Merge (push-first):** `git push` → `gh pr merge --rebase` → checkout+pull
  main → grep main for markers (`resolve_by_bot("silence"`, `_rearm_panel_episodes`,
  `notified_only`, `get_open_by_id`).

---

## Self-review notes
- Task ordering: 1 changes `_evaluate_bot`'s signature (date) before 3/4/6 which also edit
  that function — implement 1 first, then 3,4,6 build on the new shape.
- Tasks 3 and 8 are the two that touch `incident_tracker.py` (a method add each); both get
  unit tests in `tests/test_incident_tracker.py`. All others are `mcp_watcher.py`/`storage.py`.
- Reproduced-by-execution bugs (highest confidence): Task 1 (stale normal), Task 2
  (error-closes-own-incident), Task 5 (restart orphan). Land and verify those first if
  splitting the PR.
- Every `fixable` derivation must be shared between the alert path and the dedupe re-open
  (Task 4) — extract one helper, don't duplicate the rule.
