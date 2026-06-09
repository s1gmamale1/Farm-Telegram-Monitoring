# Fix Campaign Phase A — Kill the Alert Storm — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dead PC produces exactly ONE PC-off HIGH per episode (plus the tracker's
scheduled followups/give-up), recovery always produces exactly one closure signal, and
watcher restarts re-alert nothing — by stopping the watcher from counting its own
outgoing probes as panel activity and by making the two PC-off paths share one episode.

**Architecture:** Five surgical changes in `watcherdog/tg_tools.py` (outgoing-message
filter), `watcherdog/panel_rules.py` (one new PanelState field), and
`watcherdog/mcp_watcher.py` (`_evaluate_panel` reroute/latches + `_handle_panel_selfreport_silence`).
Spec: `docs/superpowers/specs/2026-06-10-deep-review-fix-campaign-design.md`.
Root production evidence: SinFermera2 alerted PC-off HIGH 10× at an exact ~71-min period
(stale 70m + one 120s sweep); SinFermera16 double-alerted at 00:06 (R6) + 00:08 (R5).

**Tech Stack:** Python 3 / Telethon / pytest. No new dependencies.

**Environment (read first):**
- Work in a git worktree off `main`: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-phase-a -b fix/phase-a-alert-storm main`
- The venv lives ONLY in the main checkout. Run tests from the worktree as:
  `cd /tmp/wd-phase-a && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`
- The authoritative suite is tracked files only: `pytest $(git ls-files 'tests/*.py')`.
  Never run bare `pytest` in the MAIN checkout (untracked stale gaps files fail there).
- Before merging: `git push` the branch FIRST, then `gh pr merge`, then grep main for a
  marker line from the fix (e.g. `getattr(m, "out", False)`).

---

### Task 1: `latest_message` skips the watcher's own outgoing messages

The watcher's `/start` probe currently becomes the panel's "latest activity", resetting
the staleness clock every probe (the 71-minute alert storm). Fetch a few messages and
return the first *incoming* one. `roster.py` (hourly statuses) gets the fix for free.

**Files:**
- Modify: `watcherdog/tg_tools.py:128-143` (`latest_message`)
- Test: `tests/test_tg_tools.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tg_tools.py` (match the file's existing fake-client style; if it
has no fake client for `get_messages`, use this self-contained one):

```python
import asyncio
from types import SimpleNamespace

from watcherdog import tg_tools


class _FakeClient:
    def __init__(self, msgs):
        self._msgs = msgs

    async def get_messages(self, ent, limit=1):
        return self._msgs[:limit]


def _msg(text, ts, out=False):
    return SimpleNamespace(message=text, date=SimpleNamespace(timestamp=lambda: ts),
                           out=out)


def test_latest_message_skips_own_outgoing_probe():
    # Newest message is OUR outgoing /start probe; the panel's real latest is the
    # older incoming status. The probe must NOT count as panel activity.
    probe = _msg("/start", 1000.0, out=True)
    real = _msg("📊 Panel status: ...", 900.0, out=False)
    client = _FakeClient([probe, real])
    text, date = asyncio.run(tg_tools.latest_message(client, object()))
    assert text == "📊 Panel status: ..."
    assert date.timestamp() == 900.0


def test_latest_message_all_outgoing_returns_empty():
    # A chat where the only recent messages are ours (e.g. repeated probes into a
    # dead chat) has NO panel activity — report none rather than our own traffic.
    client = _FakeClient([_msg("/start", 1000.0, out=True),
                          _msg("/start", 800.0, out=True)])
    text, date = asyncio.run(tg_tools.latest_message(client, object()))
    assert text == "" and date is None


def test_latest_message_incoming_unchanged():
    client = _FakeClient([_msg("🎁 collected drop", 1000.0, out=False)])
    text, date = asyncio.run(tg_tools.latest_message(client, object()))
    assert text == "🎁 collected drop"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_tg_tools.py -k latest_message -v`
Expected: the two new outgoing tests FAIL (probe text returned); incoming test may pass.

- [ ] **Step 3: Implement the filter**

In `watcherdog/tg_tools.py`, replace the body of `latest_message`:

```python
async def latest_message(client, ent, mark_read=False):
    """(text, date) of the entity's most recent INCOMING message, or ("", None).

    Skips the watcher's own outgoing messages (liveness probes, button presses):
    counting our own /start as "panel activity" resets the staleness clock and
    breaks the one-alert-per-episode latch. Fetches a small window so a probe
    sitting on top never hides the panel's real latest message.

    When `mark_read`, also acknowledge the chat as read so its unread badge
    clears (the watcher has now read it on the owner's behalf)."""
    try:
        msgs = await client.get_messages(ent, limit=5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read failed for %s: %s", entity_name(ent), exc)
        return "", None
    msgs = list(msgs)
    if mark_read and msgs:
        await mark_chat_read(client, ent)
    for m in msgs:
        if not getattr(m, "out", False):
            return (m.message or ""), m.date
    return "", None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_tg_tools.py tests/test_roster.py -v`
Expected: PASS (roster tests confirm no regression for the hourly-status consumer).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/tg_tools.py tests/test_tg_tools.py
git commit -m "fix(tg_tools): latest_message skips own outgoing probes (71-min alert storm)"
```

---

### Task 2: R5 self-report handler honors `flag_alerted` and the seed sweep

R6's PC-off branch sets `ps.flag_alerted = True` but `_handle_panel_selfreport_silence`
only checks `coldcase_reported`, so a watchdog notice arriving minutes after an R6 alert
re-probes and re-alerts (SinFermera16 00:06 + 00:08). It also runs on the FIRST sweep
after a restart (no seed guard) and on hours-old notices read back at startup.

**Files:**
- Modify: `watcherdog/mcp_watcher.py:405-408` (reroute) and `:626-697` (handler)
- Test: `tests/test_evaluate_panel.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evaluate_panel.py` (uses the existing `_cfg`/`_run`/`_clear_state`
helpers; `SELFREPORT` mirrors the production notice):

```python
SELFREPORT = ("⚠️Panel has not sent any messages for 2 hours. Please check it!⚠️")


def test_selfreport_respects_r6_pc_off_latch(monkeypatch):
    # R6 already alerted PC-off this episode (flag_alerted latched). A self-report
    # notice minutes later must NOT probe again or re-alert.
    alerts, probes = [], []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)
        return True

    async def fake_responds(client, target_ref, cfg):
        probes.append(target_ref)
        return False

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher, "_panel_responds", fake_responds)

    ps = mcp_watcher.panel_rules.PanelState()
    ps.flag_alerted = True
    mcp_watcher._PANEL_STATE["SinFermera16"] = ps

    note = _run(_cfg(), SELFREPORT, name="SinFermera16", target="ibo")
    assert note == "self-report: PC-off already alerted"
    assert probes == [] and alerts == []


def test_selfreport_seed_sweep_is_quiet(monkeypatch):
    probes = []

    async def fake_responds(client, target_ref, cfg):
        probes.append(target_ref)
        return False

    monkeypatch.setattr(mcp_watcher, "_panel_responds", fake_responds)
    note = _run(_cfg(), SELFREPORT, name="SinFermera16", seed=True)
    assert note == "self-report: seeded"
    assert probes == []


def test_selfreport_stale_notice_defers_to_r6(monkeypatch):
    # A notice OLDER than panel_stale_minutes (e.g. read back right after a watcher
    # restart) must not short-circuit into an immediate probe+alert: the age-based
    # R6 path (with its own seed guard and probe debounce) owns old traffic.
    probes = []

    async def fake_responds(client, target_ref, cfg):
        probes.append(target_ref)
        return False

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        return True

    monkeypatch.setattr(mcp_watcher, "_panel_responds", fake_responds)
    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    # age = 2x the stale window -> reroute must NOT call the self-report handler.
    cfg = _cfg(panel_stale_minutes=30)
    note = _run(cfg, SELFREPORT, name="SinFermera16", age=3600.0, seed=True)
    # seed=True: R6 seeds quietly; the point is the R5 handler did not probe.
    assert probes == []
    assert note != "self-report: PC off"


def test_selfreport_dead_still_alerts_once_then_latches(monkeypatch):
    # Sanity: with NO prior episode, the dead path still alerts exactly once and
    # latches; a second notice in the same episode stays quiet.
    alerts = []

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)
        return True

    async def fake_responds(client, target_ref, cfg):
        return False

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    monkeypatch.setattr(mcp_watcher, "_panel_responds", fake_responds)

    assert _run(_cfg(), SELFREPORT, name="SinFermera11") == "self-report: PC off"
    assert len(alerts) == 1
    assert _run(_cfg(), SELFREPORT, name="SinFermera11") == \
        "self-report: PC-off already alerted"
    assert len(alerts) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py -k selfreport -v`
Expected: FAIL — `flag_alerted` test gets a probe + second alert; seed test returns
"self-report: PC off"; stale test probes.

- [ ] **Step 3: Implement**

In `watcherdog/mcp_watcher.py`, change the reroute (currently lines 405-408) to gate on
notice age and pass `seed`:

```python
    if is_panel_silence_selfreport(text):
        note_age = (time.time() - date.timestamp()) if date else None
        if note_age is None or note_age < cfg.panel_stale_minutes * 60:
            return await _handle_panel_selfreport_silence(
                client, cfg, name, target_ref, deliver=deliver, state=state,
                target=target, ent=ent, seed=seed)
        # Stale notice (e.g. read back after a restart): fall through — the
        # age-based R6 path below owns old traffic (seed guard, probe debounce).
```

In `_handle_panel_selfreport_silence`, add the `seed` parameter and the two guards right
after the dry-run guard / before the probe:

```python
async def _handle_panel_selfreport_silence(client, cfg, name, target_ref, *,
                                           deliver, state, target, ent, seed=False):
```

and after the existing `if ps.coldcase_reported:` early-return:

```python
    if seed:
        # First sweep after (re)start: never probe/alert off a notice we may have
        # already handled before the restart — mirror R6's seed-quiet behavior.
        return "self-report: seeded"
    # R6 (or a previous notice) already alerted PC-off this episode — the latch is
    # shared between BOTH PC-off paths so one dead PC yields one HIGH.
    if ps.flag_alerted:
        return "self-report: PC-off already alerted"
```

NOTE: keep the seed check BEFORE the flag_alerted check, and both AFTER the
`coldcase_reported` return, preserving the existing order dry-run → coldcase.

- [ ] **Step 4: Run the panel test files**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py tests/test_mcp_watcher_core.py -v`
Expected: all PASS (existing R5 tests still green — they don't pre-set flag_alerted).

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_evaluate_panel.py
git commit -m "fix(monitor): R5 self-report shares the PC-off latch + seed/age guards"
```

---

### Task 3: Healthy recovery resets the full latch set (`r2_attempted_ts`, `last_action_ts`)

The healthy-recovery block resets `recover_attempts`/`episode_issue`/`coldcase_reported`
but not `r2_attempted_ts`/`last_action_ts`, so a LATER episode's first R2 decision sees a
stale `r2_attempted_ts` and runs the R4 black-screen check BEFORE attempting a single
relaunch — inverting the designed order (relaunch first, screenshot only if it didn't take).

**Files:**
- Modify: `watcherdog/mcp_watcher.py:447-449`
- Test: `tests/test_evaluate_panel.py`

- [ ] **Step 1: Write the failing test**

```python
def test_recovery_resets_r2_and_action_timestamps(monkeypatch):
    # Episode 1 relaunched (r2_attempted_ts armed). Panel goes healthy. A NEW
    # under-launch episode hours later must RELAUNCH first — not run the R4
    # black-screen check off the stale timestamp.
    ran, shots = [], []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    async def fake_shot(client, target_ref, cfg):
        shots.append(target_ref)
        return {"black": True}

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mcp_watcher.panel_actions, "screenshot_black", fake_shot)

    ps = mcp_watcher.panel_rules.PanelState()
    ps.r2_attempted_ts = time.time() - 7200   # stale: armed 2h ago
    ps.last_action_ts = time.time() - 7200
    ps.recover_attempts = 1
    mcp_watcher._PANEL_STATE["SinFermera8"] = ps

    assert _run(_cfg(), HEALTHY, name="SinFermera8") is None   # healthy recovery
    assert mcp_watcher._PANEL_STATE["SinFermera8"].r2_attempted_ts is None
    assert mcp_watcher._PANEL_STATE["SinFermera8"].last_action_ts is None

    note = _run(_cfg(), UNDER, name="SinFermera8")              # new episode
    assert shots == []                       # no pre-relaunch screenshot
    assert ran and ran[0][:2] == ["select_unfarmed", "start_selected"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py::test_recovery_resets_r2_and_action_timestamps -v`
Expected: FAIL — `r2_attempted_ts` survives recovery and `shots` is non-empty.

- [ ] **Step 3: Implement**

In the healthy-noop branch of `_evaluate_panel` (currently lines 447-449), extend the
reset block:

```python
            ps.recover_attempts = 0
            ps.episode_issue = None
            ps.coldcase_reported = False
            ps.r2_attempted_ts = None
            ps.last_action_ts = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_evaluate_panel.py
git commit -m "fix(monitor): healthy recovery resets r2/action timestamps (no pre-relaunch R4)"
```

---

### Task 4: A cold-cased panel that resumes posting after a silence gap starts a new episode

`coldcase_reported` clears only on a fully-HEALTHY card, so a PC that the owner power-cycles
and that comes back at "2/4 OFFLINE" (the exact state the cold case escalated for) is
permanently ignored. Distinguish "PC came back" (silence gap > stale window, then fresh
parseable card → new episode, act again) from "same futile state still posting" (R4/
retry-cap cold case with continuous posting → stay latched).

**Files:**
- Modify: `watcherdog/panel_rules.py:14-26` (new `last_msg_ts` field)
- Modify: `watcherdog/mcp_watcher.py` (`_evaluate_panel`, before the `coldcase_reported`
  early-return at current line 454)
- Test: `tests/test_evaluate_panel.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_coldcase_unlatches_after_silence_gap_with_fresh_card(monkeypatch):
    # Cold case latched, panel silent > stale window (PC off), then a FRESH
    # parseable-but-unhealthy card arrives (PC powered back on at 2/4): the FSM
    # must start a new episode and relaunch instead of staying quiet forever.
    ran = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)

    cfg = _cfg(panel_stale_minutes=30)
    now = time.time()
    ps = mcp_watcher.panel_rules.PanelState()
    ps.coldcase_reported = True
    ps.recover_attempts = 3
    ps.episode_issue = "under-launch"
    ps.last_msg_ts = now - 3600           # last seen message: 60 min ago (> 30m gap)
    mcp_watcher._PANEL_STATE["SinFermera2"] = ps

    note = _run(cfg, UNDER, name="SinFermera2", age=5.0)   # fresh card now
    assert ran, f"expected a relaunch, got note={note!r}"
    assert mcp_watcher._PANEL_STATE["SinFermera2"].coldcase_reported is False


def test_coldcase_stays_latched_while_panel_keeps_posting(monkeypatch):
    # R4/retry-cap cold case where the panel KEEPS posting (no silence gap):
    # the latch must hold — this is the same futile episode, not a power-cycle.
    ran = []

    async def fake_seq(client, panel, actions, cfg, *, confirmed=True):
        ran.append(actions)
        return [{"ok": True} for _ in actions]

    monkeypatch.setattr(mcp_watcher.panel_actions, "run_sequence", fake_seq)

    cfg = _cfg(panel_stale_minutes=30)
    now = time.time()
    ps = mcp_watcher.panel_rules.PanelState()
    ps.coldcase_reported = True
    ps.recover_attempts = 3
    ps.last_msg_ts = now - 120            # posted 2 min ago: no gap
    mcp_watcher._PANEL_STATE["SinFermera13"] = ps

    note = _run(cfg, UNDER, name="SinFermera13", age=5.0)
    assert note == "cold-case: awaiting PC"
    assert ran == []
    assert mcp_watcher._PANEL_STATE["SinFermera13"].coldcase_reported is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py -k coldcase -v`
Expected: first test FAILS (`PanelState` has no `last_msg_ts`; then note ==
"cold-case: awaiting PC" with no relaunch). Second may error on the missing field —
that's fine, both go green in Step 4.

- [ ] **Step 3: Implement**

(a) `watcherdog/panel_rules.py` — add one field to `PanelState` (after `last_probe_ts`):

```python
    last_msg_ts: float | None = None     # ts of the panel's latest INCOMING message
                                         # seen by _evaluate_panel (gap detection)
```

(b) `watcherdog/mcp_watcher.py`, in `_evaluate_panel`: right after
`ps = _PANEL_STATE.setdefault(name, panel_rules.PanelState())` (current line 420), track
the gap BEFORE overwriting:

```python
    prev_msg_ts = ps.last_msg_ts
    if date:
        ps.last_msg_ts = date.timestamp()
```

(c) Immediately BEFORE the `if ps.coldcase_reported:` early-return (current line 454),
insert the new-episode reset:

```python
    # A cold-cased panel that went silent past the stale window and is now posting
    # parseable cards again demonstrably had its PC come back (power-cycle) — start
    # a NEW episode so the FSM may act again. A cold case that kept posting all
    # along (R4 / retry-cap, same futile state) stays latched.
    if (ps.coldcase_reported and status is not None and date
            and prev_msg_ts is not None
            and (date.timestamp() - prev_msg_ts) > cfg.panel_stale_minutes * 60):
        ps.coldcase_reported = False
        ps.recover_attempts = 0
        ps.episode_issue = None
```

- [ ] **Step 4: Run the panel suite**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py tests/test_panel_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/panel_rules.py watcherdog/mcp_watcher.py tests/test_evaluate_panel.py
git commit -m "fix(monitor): cold case unlatches when the PC demonstrably returns"
```

---

### Task 5: Closure consults the durable ledger, not just the in-memory latch

`had_episode` reads only the in-memory latches, which Task 1's bug (and any restart) wipes
— so recoveries skipped `_resolve_incidents_for` and open `panel:` rows leaked for 30+ h.
Make the healthy branch ALSO resolve when the tracker has an open panel row.

**Files:**
- Modify: `watcherdog/mcp_watcher.py:430-449` (healthy-noop branch)
- Test: `tests/test_evaluate_panel.py`

- [ ] **Step 1: Write the failing test**

```python
def test_healthy_resolves_open_ledger_row_even_without_latches(monkeypatch):
    # Latches wiped (restart / old latch bug) but the ledger still holds an open
    # panel incident. A healthy card must close it and announce once.
    resolved, alerts = [], []

    class _FakeTracker:
        def open_for_bot(self, source, bot):
            return {"key": f"panel:{bot}"} if source == "panel" else None

        def resolve_open_for_bot(self, bot, resolution, now=None):
            resolved.append((bot, resolution))
            return {"count": 1, "elapsed": 120.0, "we_fixed": False}

    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        alerts.append(text)
        return True

    monkeypatch.setattr(mcp_watcher, "_alert", fake_alert)
    state = {"tracker": _FakeTracker()}

    assert _run(_cfg(), HEALTHY, name="SinFermera24", state=state) is None
    assert resolved == [("SinFermera24", "self_healed")]
    assert len(alerts) == 1 and "Resolved" in alerts[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py::test_healthy_resolves_open_ledger_row_even_without_latches -v`
Expected: FAIL — `resolved == []` (had_episode is False, resolve skipped).

- [ ] **Step 3: Implement**

In the healthy-noop branch, widen the gate (replace `if had_episode:` at current
line 444):

```python
            tracker = state.get("tracker")
            if not had_episode and tracker is not None:
                # Latches are process-memory and can be wiped (restart); the open
                # ledger row is the durable episode identity — honor it too.
                had_episode = tracker.open_for_bot("panel", name) is not None
            if had_episode:
                await _resolve_incidents_for(state, client, target, name, now, deliver, cfg,
                                             announce=announce_resolved)
```

(The `open_for_bot` SELECT runs only when the latches say "no episode", so plain-healthy
panels still skip per-sweep queries — preserving the original comment's intent.)

- [ ] **Step 4: Run the panel + lifecycle suites**

Run: `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_evaluate_panel.py tests/test_incident_tracker.py tests/test_mcp_watcher_core.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add watcherdog/mcp_watcher.py tests/test_evaluate_panel.py
git commit -m "fix(monitor): healthy closure honors the durable open incident row"
```

---

### Task 6: Full-suite gate, PR, review

- [ ] **Step 1: Run the complete tracked suite**

Run (from the worktree):
`/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`
Expected: 0 failures (baseline before this phase: 952 passed / 2 skipped; new tests add ~10).

- [ ] **Step 2: Push the branch and open the PR**

```bash
git push -u origin fix/phase-a-alert-storm
gh pr create --title "fix(monitor): Phase A — kill the PC-off alert storm" --body "$(cat <<'EOF'
## Summary
- latest_message skips the watcher's own outgoing probes (root cause of the ~71-min
  re-alert loop: 10 PC-off HIGHs for one dead PC)
- R5 self-report shares R6's PC-off latch + seed/age guards (kills dual-path doubles
  and restart re-floods)
- healthy recovery resets r2/action timestamps (no pre-relaunch black-screen check)
- cold case unlatches when the PC demonstrably returns (silence gap + fresh card)
- closure honors the durable open incident row (no more 30h leaked incidents)

Spec: docs/superpowers/specs/2026-06-10-deep-review-fix-campaign-design.md (Phase A)

## Test plan
- [x] regression tests per fix (probe-as-latest, shared latch, seed/stale notice,
  timestamp resets, gap-unlatch, ledger-driven closure)
- [x] tracked suite green: pytest $(git ls-files 'tests/*.py')

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Reviewer pass** — dispatch the code-review flow (/code-review) on the PR;
fix findings; re-run the tracked suite.

- [ ] **Step 4: Merge (push-first discipline)**

```bash
git push                                  # ALWAYS push the final commit first
gh pr merge --rebase                      # then merge
git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring checkout main
git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring pull
grep -n 'getattr(m, "out", False)' /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/watcherdog/tg_tools.py   # marker: fix actually on main
```
