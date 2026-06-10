# Fix Campaign Phase D — Infra Hardening (high-value subset) — Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the three infra failure modes with real data-loss / operational risk: a
transient at the weekly drop-stats run silently losing a week of stats; an oversized LLM
alert being dropped instead of delivered; and the restart supervisor rolling back a good
self-edit on a slow boot or double-starting into a corrupt `watcher.session`.

**Scoping note.** The 2026-06-10 review's "Tier 3 infra" bucket had ~11 items. This phase
ships the **3 with genuine operational/data-loss risk**. The other 8 are consciously deferred
to `WISHLIST.md` with rationale (Special Forces hardening is moot — default-off since PR #6;
`IncidentStore.busy_timeout` is theoretical under the single-thread design; escalation-copy /
`_alert`-consistency are cosmetic; per-bot dedupe scope, blocking-I/O executor offload,
`dispatch_bots` lock, and the daily-report clear race are real but low-frequency). Build those
when their trigger arrives. Spec:
`docs/superpowers/specs/2026-06-10-deep-review-fix-campaign-design.md` (Phase D).
Phases A+B+C merged (main `888fe91`); branch off current main.

**Tech Stack:** Python 3 / Telethon / pytest. No new dependencies.

**Environment (same discipline as A/B/C):**
- Worktree: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-phase-d -b fix/phase-d-infra main`
- Tests use the main venv: `cd /tmp/wd-phase-d && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`. NEVER bare `pytest`.
- Authoritative suite: `pytest $(git ls-files 'tests/*.py')`. Worktree baseline ~978 green
  (the main-checkout count is higher due to data-fixture-parametrized tests whose `data/` dir
  doesn't propagate to worktrees — not a regression; compare within-worktree before/after).
- Merge: `git push` FIRST, then `gh pr merge --rebase`, then grep main for a marker.
- READ the target test file before adding tests; reuse existing helpers.

**Key facts (verified against main `888fe91`):**
- `drop_stats.run_weekly` (375-399): on `panels == []` it only `log.warning`s, then STILL calls
  `collect_week` (empty rows) → `write_buffer` (OVERWRITES the week's `<week>.json` with `[]`) →
  `push_to_sheets([])` (reports "Total: 0 · saved ✅") → reports success → `weekly_loop`
  re-arms 7 days out. No alert, no retry.
- `drop_stats.weekly_loop` (402-414): `sleep(seconds_until(...))` → `run_weekly` (try/except
  logs) → `sleep(60)`. A failed run waits a FULL WEEK to retry.
- `tg_tools._resolve_folder` (57-70): line 63 strips the WANTED name, but line 67 compares the
  LIVE `filter_title(flt).lower()` UN-stripped → a folder titled "Panels " (trailing space)
  never matches → zero panels.
- `alerter.format_alert` (27-50): caps only the `excerpt` to 1200 chars; `summary`/`root_cause`/
  `fix` come UNCAPPED from the LLM. The existing `test_format_alert_truncates_long_excerpt` uses
  an empty `{}` analysis, so the uncapped-fields path is untested. A >4096 assembled alert →
  Telethon `MessageTooLongError` → caught → `False` → alert silently lost.
- `self_restart._spec` (111-120): `"health_timeout": 45`. A slow Telethon cold start (FloodWait)
  can exceed 45s → a VALID self-edit gets rolled back while the bot said "Validated ✅".
- `restart_helper.main` (109-130): after `_stop(old_pid)` then `new_pid = _start(...)` — if
  `_start` raises, the old process is dead and nothing relaunches. The fallback `_start` (129)
  gets no health check. Two near-simultaneous `request_restart` → two specs (same path) → two
  supervisors → two `_start`s → two watchers on one `watcher.session` (the known corrupt-session
  failure class — see memory `tg-login-failure-was-corrupt-session`).

---

### Task 1: weekly drop-stats treats zero panels as a failure (no clobber, alert, retry)

**Files:**
- Modify: `watcherdog/drop_stats.py` — `run_weekly` (zero-panel guard) + `weekly_loop` (retry)
- Modify: `watcherdog/tg_tools.py:67` — `.strip()` the live folder title
- Test: `tests/test_drop_stats.py`, `tests/test_tg_tools.py` (or `test_tg_tools_async.py`)

- [ ] **Step 1: Write failing tests.**
  (a) `tests/test_drop_stats.py` — `run_weekly` with `load_panels` returning `[]` must: NOT call
  `write_buffer`, NOT call `push_to_sheets`, send a FAILURE alert (or return a failure marker),
  and NOT report success. Monkeypatch `load_panels` → `[]`, `write_buffer` → recorder,
  `push_to_sheets` → recorder, `_send` → recorder. Assert `write_buffer`/`push_to_sheets` were
  NOT called and the returned dict signals failure (e.g. `{"ok": False, "reason": "no panels"}`
  or `rows is None`), and the sent text mentions the failure.
  ```python
  def test_run_weekly_zero_panels_does_not_clobber_or_push(tmp_path, monkeypatch):
      cfg = _cfg(tmp_path)                      # reuse the file's cfg helper
      wrote, pushed, sent = [], [], []
      async def no_panels(client, cfg): return []
      monkeypatch.setattr(drop_stats, "load_panels", no_panels)
      monkeypatch.setattr(drop_stats, "write_buffer", lambda *a, **k: wrote.append(a))
      monkeypatch.setattr(drop_stats, "push_to_sheets", lambda *a, **k: pushed.append(a))
      async def fake_send(client, target, text, deliver=True): sent.append(text)
      monkeypatch.setattr(drop_stats, "_send", fake_send)
      res = asyncio.run(drop_stats.run_weekly(object(), cfg, target="ibo", deliver=True))
      assert wrote == [] and pushed == []          # week buffer NOT clobbered, nothing pushed
      assert res.get("ok") is False                # failure signalled
      assert sent and ("no panels" in sent[0].lower() or "couldn't" in sent[0].lower())
  ```
  Match the real `run_weekly` return shape — read it; the current return is
  `{"week","rows","push","path"}`. Decide the failure shape (add `"ok": False` and skip the
  rows/push keys, or set them to None) and make the test assert exactly that.
  (b) `tests/test_tg_tools.py` (or `_async`) — `_resolve_folder` matches a folder whose live title
  has surrounding whitespace ("Panels ") against `folder_ref="Panels"`. Use the existing
  GetDialogFilters fake pattern in that test file (find it); if none exists, a focused unit test
  constructing a fake filter object with `title="Panels "` and asserting `_resolve_folder` returns
  it.
- [ ] **Step 2:** Run, confirm (a) FAILS (today it clobbers + pushes + reports success) and (b)
  FAILS (trailing-space title doesn't match).
- [ ] **Step 3: Implement.**
  - `drop_stats.run_weekly` — replace the zero-panel handling:
    ```python
    panels = await load_panels(client, cfg)
    if not panels:
        log.warning("No panels resolved in folder %r — NOT collecting (would clobber "
                    "the week buffer with empty data); alerting + leaving last good data.",
                    cfg.panels_folder)
        msg = (f"⚠️ Weekly drop-stats ({week}): could not read the '{cfg.panels_folder}' "
               "folder (0 panels). Skipped — last week's buffer is preserved. "
               "Check the folder name / connection and re-run.")
        if target is not None:
            await _send(client, target, msg, deliver)
        return {"week": week, "ok": False, "reason": "no panels",
                "rows": None, "push": None, "path": None}
    ```
    Keep the existing success path below for the non-empty case (it should also return
    `"ok": True` for symmetry — add that key to the success return).
  - `drop_stats.weekly_loop` — retry sooner on a failed/zero-panel run instead of waiting a week:
    ```python
        try:
            res = await run_weekly(client, cfg, target, deliver=deliver)
        except Exception:  # noqa: BLE001
            log.exception("weekly drop-stats run failed; will retry in 1h")
            res = {"ok": False}
        if not res.get("ok"):
            await asyncio.sleep(3600)   # transient (folder/conn) — retry in an hour, not a week
            continue
        await asyncio.sleep(60)         # success: step past the trigger minute before re-arming
    ```
    (Confirm `weekly_loop`'s loop structure; keep the `seconds_until` sleep at the TOP unchanged
    so a SUCCESS still waits until next Wednesday — only a FAILURE short-retries.)
  - `tg_tools.py:67`: `if want_name is not None and filter_title(flt).strip().lower() == want_name:`
- [ ] **Step 4:** Run `tests/test_drop_stats.py tests/test_tg_tools.py tests/test_tg_tools_async.py` — all pass.
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(drop-stats): zero panels = failure (no clobber, alert, hourly retry); strip folder title`.

---

### Task 2: oversized alerts are truncated, never dropped

**Files:**
- Modify: `watcherdog/alerter.py:format_alert` (final assembled-length cap)
- Test: `tests/test_alerter.py`

- [ ] **Step 1: Write the failing test.** The existing `test_format_alert_truncates_long_excerpt`
  only caps the excerpt with an empty analysis. Add one for the UNCAPPED fields:
  ```python
  def test_format_alert_caps_total_length_with_huge_llm_fields():
      # summary/root_cause/fix come uncapped from the LLM; the assembled alert must
      # still fit under Telegram's 4096-char limit or it gets dropped (MessageTooLong).
      analysis = {"summary": "s" * 5000, "root_cause": "r" * 5000, "fix": "f" * 5000}
      out = format_alert("bot", "critical", analysis, "x" * 5000)
      assert len(out) <= 4096
      assert out.startswith("🔴" ) or "Bot Error Detected" in out   # header preserved
  ```
  (Use the real critical-severity emoji from `_SEVERITY_EMOJI` if the prefix assert is brittle —
  prefer asserting `"Bot Error Detected" in out[:120]` so the header survives the truncation.)
- [ ] **Step 2:** Run, confirm it FAILS (assembled length > 4096 today).
- [ ] **Step 3: Implement.** In `format_alert`, after building the message, cap the assembled
  result, preserving the header by truncating the TAIL:
  ```python
      msg = "\n".join(lines)
      # Final safety net: summary/root_cause/fix are uncapped LLM text; an over-limit
      # message raises MessageTooLong and the alert is lost. Keep the header + as much
      # body as fits, well under Telegram's 4096.
      LIMIT = 4000
      if len(msg) > LIMIT:
          msg = msg[:LIMIT - 1].rstrip() + "…"
      return msg
  ```
  (Replace the current `return "\n".join(lines)`.)
- [ ] **Step 4:** Run `tests/test_alerter.py` — all pass (the existing excerpt-cap test still green).
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(alerter): cap assembled alert length so an oversized LLM alert is truncated, not dropped`.

---

### Task 3: restart supervisor — raise the health timeout + a singleton lock against double-start

**Files:**
- Modify: `watcherdog/self_restart.py:119` (health_timeout 45→90)
- Modify: `watcherdog/restart_helper.py` — a singleton lock at the top of `main()`, and a
  try/except around the primary `_start` so a failed start still attempts recovery
- Test: `tests/test_restart_helper.py`, `tests/test_self_restart.py`

- [ ] **Step 1: Write failing tests.**
  (a) `tests/test_self_restart.py` — `_spec(...)`'s `health_timeout` is now >= 90 (a slow cold
  start must not be rolled back). Build a cfg with the fields `_spec` reads (look at the existing
  `_spec` tests / `test_valid_code_launches_the_supervisor` for the cfg shape) and assert
  `spec["health_timeout"] >= 90`.
  (b) `tests/test_restart_helper.py` — a new `_acquire_singleton_lock(path)` returns True for the
  first caller and False for a second caller while the first holds it (atomic O_CREAT|O_EXCL);
  releasing/stale-removing lets a later caller acquire. Mirror the file's `tmp_path` helper style.
  ```python
  def test_singleton_lock_blocks_a_second_helper(tmp_path):
      lock = str(tmp_path / "restart.lock")
      assert restart_helper._acquire_singleton_lock(lock) is True
      assert restart_helper._acquire_singleton_lock(lock) is False   # second helper backs off
  ```
- [ ] **Step 2:** Run, confirm both FAIL (`health_timeout` is 45; `_acquire_singleton_lock` doesn't exist).
- [ ] **Step 3: Implement.**
  - `self_restart.py:119`: `"delay": 6, "health_timeout": 90,` (a slow boot no longer rolls back a
    good edit). Update the inline comment if any.
  - `restart_helper.py` — add the lock helper (atomic create; write our pid; a stale lock older
    than a generous TTL is reclaimed):
    ```python
    def _acquire_singleton_lock(path, ttl=300):
        """True if we got the lock; False if another live supervisor holds it. Atomic
        O_CREAT|O_EXCL create; a lock older than ttl (a crashed prior helper) is reclaimed."""
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > ttl:
                    os.remove(path)               # stale — prior helper died
                    return _acquire_singleton_lock(path, ttl)
            except OSError:
                pass
            return False
    ```
    At the TOP of `main()` (after loading the spec), acquire a lock next to the spec
    (`lock_path = sys.argv[1] + ".lock"`); if not acquired, exit immediately (another supervisor
    is already handling the restart). Release it (`_drop(lock_path)`) on both the healthy-exit and
    the rollback-exit paths. Also wrap the primary `_start` so a raise there still attempts a
    recovery `_start` rather than leaving nothing running:
    ```python
        if not _acquire_singleton_lock(sys.argv[1] + ".lock"):
            return   # another supervisor is already restarting — don't double-start
        try:
            _stop(spec["pid"])
            since = time.time()
            try:
                new_pid = _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])
            except Exception:  # noqa: BLE001
                # the start itself failed — try once more so we don't leave nothing running
                new_pid = _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])
            if _wait_healthy(spec["health_path"], since, new_pid, spec.get("health_timeout", 90)):
                _drop(spec["edits_path"]); _drop(sys.argv[1]); return
            _stop(new_pid); _rollback(spec["edits_path"]); _drop(spec["edits_path"])
            _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])
            _drop(sys.argv[1])
        finally:
            _drop(sys.argv[1] + ".lock")
    ```
    Keep the existing default fallback `spec.get("health_timeout", ...)` aligned with the new 90.
- [ ] **Step 4:** Run `tests/test_restart_helper.py tests/test_self_restart.py` — all pass
  (existing `_wait_healthy`/`_rollback`/`_stop` tests untouched and green).
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `fix(restart): raise health timeout + singleton lock against double-start (corrupt-session class)`.

---

### Task 4: wishlist the deferred items, full-suite gate, PR, holistic review, merge

- [ ] **Step 1:** Update `WISHLIST.md` — under the "Follow-ups from the fix campaign" / a new
  "Phase D deferred (low-value, build-on-trigger)" note, list the 8 deferred items with their
  `file:line` + the trigger to build each (SF hardening → "when SPECIAL_FORCES re-enabled";
  busy_timeout → "if a CLI/2nd process ever touches the DB live"; per-bot dedupe → "if a
  fleet-wide untagged error recurs"; blocking I/O → "when GSHEETS configured"; dispatch_bots lock
  → "if concurrent fan-out button races appear"; daily-report clear race; escalation copy/duration
  cosmetics; `_alert` fixed-report consistency). Commit this doc change.
- [ ] **Step 2:** `pytest $(git ls-files 'tests/*.py')` — 0 failures.
- [ ] **Step 3:** push `fix/phase-d-infra`; open PR.
- [ ] **Step 4:** holistic review across the 3 fix commits — focus: does the drop-stats
  `weekly_loop` retry-in-1h loop ever tight-loop (the `seconds_until` top sleep guards the success
  path; the failure path sleeps 3600 then `continue`s back to the TOP which re-sleeps
  `seconds_until` — confirm a persistent failure retries hourly, not instantly, and doesn't skip
  the next legit Wednesday); does the restart lock release on every exit path (finally); does the
  alert cap ever cut a multibyte char mid-sequence (truncating a str by chars is safe in Python 3 —
  confirm). Fix findings; re-run suite.
- [ ] **Step 5: Merge (push-first):** `git push` → `gh pr merge --rebase` → checkout+pull main →
  grep markers (`would clobber`, `health_timeout": 90`, `_acquire_singleton_lock`, `LIMIT = 4000`).

---

## Self-review notes
- Tiering: Task 1 (drop-stats — multi-branch logic + buffer-clobber risk) and Task 3 (restart —
  the corrupt-session class that bit the owner before) get FULL review. Task 2 (alert cap — a
  4-line truncation) rides on mutation-verification + the holistic pass.
- Reproduced/observed: Task 1's zero-panel path is the production 2026-06-10 00:00 warning.
- Restart `main()` runs in a DETACHED process — `_acquire_singleton_lock` and `_spec` are the
  testable units; `main()`'s wiring is verified by inspection + the helper tests.
- The deferred-8 are tracked in WISHLIST (Task 4 step 1) so nothing is lost — they're scoped and
  cited, ready to promote if their trigger arrives.
