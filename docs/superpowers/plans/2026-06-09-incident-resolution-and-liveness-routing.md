# Incident Resolution & Liveness Routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the 5 defects that make WatcherDog flood the owner with `🟠/⏳/❌` and never `✅ Resolved`: reroute the panel "silent 2h" warning into the `/start` liveness+recovery path, make resolution reachable for every incident source driven by panel health, coordinate the 3 alert channels, and make escalation wording honest.

**Architecture:** Additive changes to `incident_tracker.py` (cross-source resolve) and `alerter.py` (wording), one new regex, and wiring in `mcp_watcher.py` (`_evaluate_panel` reroute handler + unified resolution helper + channel suppression). Reuses existing `_panel_responds`, `panel_actions.run_sequence`, the `PANEL_AUTO_*` gates, and the confirm-card path — no new recovery machinery.

**Tech Stack:** Python 3 stdlib, telethon, pytest.

**Spec:** `docs/superpowers/specs/2026-06-09-incident-resolution-and-liveness-routing-design.md`

**Test command** (no `python` on PATH; from the worktree dir use the main venv):
`/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest`

---

## File Structure

- **Modify** `watcherdog/incident_tracker.py` — add `resolve_open_for_bot(bot, resolution, now)` (cross-source resolve).
- **Modify** `watcherdog/alerter.py` — add `retried` param to `format_incident_escalated`.
- **Modify** `watcherdog/classifier.py` — add `is_panel_silence_selfreport(text)` + regex (kept with the other classifiers).
- **Modify** `watcherdog/mcp_watcher.py` — reroute handler in `_evaluate_panel`; unified `_resolve_incidents_for`; trigger resolution from bot-normal / panel-healthy / silence-recovery; suppress duplicate `🟠` and `🔁` when a lifecycle incident is open; pass `retried` to the escalation formatter.
- **Modify** tests: `tests/test_incident_tracker.py`, `tests/test_alerter.py`, `tests/test_classifier.py`, `tests/test_evaluate_panel.py`, `tests/test_mcp_watcher_core.py`.

---

## Task 1: Cross-source resolve in IncidentTracker

**Files:** Modify `watcherdog/incident_tracker.py`; Test `tests/test_incident_tracker.py`

- [ ] **Step 1: Failing tests**

```python
def test_resolve_open_for_bot_closes_all_sources(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "x", fixable=False, now=100.0)
    tracker.open("panel", "Bot1", "panel:Bot1", "high", "pc", fixable=False, now=120.0)
    res = tracker.resolve_open_for_bot("Bot1", "self_healed", now=400.0)
    assert res is not None
    assert res["count"] == 2
    assert res["elapsed"] == pytest.approx(300.0)   # from EARLIEST opened_ts (100)
    assert tracker.open_list() == []

def test_resolve_open_for_bot_none_when_nothing_open(tracker):
    assert tracker.resolve_open_for_bot("Ghost", "self_healed", now=1.0) is None

def test_resolve_open_for_bot_we_fixed_when_any_attempt_fixed(tracker):
    tracker.open("bot_error", "Bot1", "bot_error:Bot1", "high", "x", fixable=True, now=100.0)
    tracker.note_fix_attempt("bot_error:Bot1", "fixed")
    res = tracker.resolve_open_for_bot("Bot1", "we_fixed", now=200.0)
    assert res["we_fixed"] is True
```

- [ ] **Step 2: Run → fail** (`AttributeError: resolve_open_for_bot`).

- [ ] **Step 3: Implement** — add to `IncidentTracker` (after `resolve_by_bot`):

```python
    def open_list_for_bot(self, bot):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE bot = ? AND status = 'open' "
            "ORDER BY opened_ts", (bot,)).fetchall()]

    def resolve_open_for_bot(self, bot, resolution, now=None):
        """Resolve ALL open incidents for a bot regardless of source (the bot/panel
        is healthy again — close everything). Returns {elapsed (from earliest
        opened_ts), we_fixed (any attempt reported 'fixed'), count} or None."""
        now = now if now is not None else time.time()
        rows = self.open_list_for_bot(bot)
        if not rows:
            return None
        earliest = min(r["opened_ts"] for r in rows)
        we_fixed = any((r["fix_attempted"] == "fixed") for r in rows)
        self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE bot = ? AND status = 'open'",
            (now, resolution, bot))
        self.conn.commit()
        return {"elapsed": now - earliest, "we_fixed": we_fixed, "count": len(rows)}
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(incident): cross-source resolve_open_for_bot`.

---

## Task 2: Honest escalation wording

**Files:** Modify `watcherdog/alerter.py`; Test `tests/test_alerter.py`

- [ ] **Step 1: Failing tests**

```python
def test_incident_escalated_retried_says_stopping():
    from watcherdog.alerter import format_incident_escalated
    m = format_incident_escalated("B", "boom", 3600, needs_pc=False, retried=True)
    assert "stopping auto-retries" in m

def test_incident_escalated_not_retried_says_no_auto_fix():
    from watcherdog.alerter import format_incident_escalated
    m = format_incident_escalated("B", "boom", 3600, needs_pc=False, retried=False)
    assert "no automatic fix available" in m
    assert "stopping auto-retries" not in m
```

- [ ] **Step 2: Run → fail** (`TypeError: unexpected keyword 'retried'`).

- [ ] **Step 3: Implement** — change `format_incident_escalated` signature/body:

```python
def format_incident_escalated(bot, summary, elapsed_seconds, *, needs_pc=False, retried=False):
    """Final give-up message. ``retried`` distinguishes 'we tried and stopped' from
    'there was never an automatic fix to try'."""
    need = "needs PC (power on / RDP)" if needs_pc else "needs manual attention"
    tail = "stopping auto-retries" if retried else "no automatic fix available"
    head = f"❌ {bot} — unresolved after {_fmt_duration(elapsed_seconds)}, {tail}"
    s = (summary or "").strip()
    if s:
        head += f"\n{s}"
    return f"{head} — {need}."
```

- [ ] **Step 4: Run → pass** (keep/adjust the existing `format_incident_escalated` test — it now needs `retried=` or relies on the default `False`; update that existing test's expected string to "no automatic fix available" if it asserted "stopping auto-retries").
- [ ] **Step 5: Commit** `feat(alerter): honest escalation wording (retried flag)`.

---

## Task 3: Detect the panel self-reported-silence warning

**Files:** Modify `watcherdog/classifier.py`; Test `tests/test_classifier.py`

- [ ] **Step 1: Failing tests**

```python
def test_panel_silence_selfreport_matches():
    from watcherdog.classifier import is_panel_silence_selfreport as f
    assert f("[SinFermera11] ⚠️Panel has not sent any messages for the last 2 hours 0 minutes. Please check it!⚠️")
    assert f("Panel has not sent any messages for the last 45 minutes. please CHECK it")

def test_panel_silence_selfreport_negatives():
    from watcherdog.classifier import is_panel_silence_selfreport as f
    assert not f("[SinFermera6] Got an error while launching accounts.")
    assert not f("All 4 accounts launched!")
    assert not f("")
```

- [ ] **Step 2: Run → fail** (ImportError).

- [ ] **Step 3: Implement** — add near the other module-level helpers in `classifier.py`:

```python
# The FSM panel's OWN watchdog notice that it has gone quiet. This is a liveness
# signal (route to a /start probe), NOT a generic error — see mcp_watcher.
_PANEL_SILENCE_SELFREPORT_RE = re.compile(
    r"has\s+not\s+sent\s+any\s+messages.*please\s+check", re.IGNORECASE | re.DOTALL)


def is_panel_silence_selfreport(text):
    """True when the panel itself reports it has gone silent ('…has not sent any
    messages… Please check it!'). Routed to the liveness/recovery path."""
    return bool(text and _PANEL_SILENCE_SELFREPORT_RE.search(text))
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(classifier): detect panel self-reported silence`.

---

## Task 4: Wire reroute + unified resolution + channel coordination into the monitor

**Files:** Modify `watcherdog/mcp_watcher.py`; Test `tests/test_evaluate_panel.py`, `tests/test_mcp_watcher_core.py`

Implement in order; run the full suite at the end. Read each anchor in the current file before editing (line numbers approximate; match on quoted code).

- [ ] **Step 1: Failing tests** (add to `tests/test_evaluate_panel.py` and `tests/test_mcp_watcher_core.py`)

In `tests/test_evaluate_panel.py` (reuse its `_cfg`/`_run` helpers + monkeypatch style):

```python
def test_selfreport_silence_alive_runs_relaunch(monkeypatch):
    import watcherdog.mcp_watcher as mw
    ran = {}
    async def fake_responds(client, ref, cfg): return True            # alive
    async def fake_seq(client, ref, actions, cfg, *, confirmed=True):
        ran["actions"] = actions
        return [{"ok": True}]
    monkeypatch.setattr(mw, "_panel_responds", fake_responds)
    monkeypatch.setattr(mw.panel_actions, "run_sequence", fake_seq)
    monkeypatch.setattr(mw.daily_report, "record", lambda *a, **k: None)
    note = _run(_cfg(panel_auto_recover=True),
                "[SinFermera7] ⚠️Panel has not sent any messages for the last 2 hours 0 minutes. Please check it!⚠️")
    assert note is not None                       # HANDLED — does not fall to _evaluate_bot
    assert ran["actions"] == ["select_unfarmed", "start_selected"]

def test_selfreport_silence_dead_reports_pc_off(monkeypatch):
    import watcherdog.mcp_watcher as mw
    sent = []
    async def fake_responds(client, ref, cfg): return False           # dead
    async def fake_alert(state, client, target, text, deliver=True, *, cfg=None):
        sent.append(text); return True
    monkeypatch.setattr(mw, "_panel_responds", fake_responds)
    monkeypatch.setattr(mw, "_alert", fake_alert)
    note = _run(_cfg(), "[SinFermera7] ⚠️Panel has not sent any messages for the last 2 hours 0 minutes. Please check it!⚠️")
    assert note is not None
    assert any("PC OFF" in s or "needs PC" in s for s in sent)
```

In `tests/test_mcp_watcher_core.py`:

```python
def test_open_incident_suppresses_duplicate_detection_alert(tmp_path, monkeypatch):
    from watcherdog.incident_tracker import IncidentTracker
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "high", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    tracker = IncidentTracker(str(tmp_path / "incidents.db"))
    tracker.open("panel", "Bot1", "panel:Bot1", "high", "pc off", fixable=False, now=1.0)
    client = _FakeClient(); state = {"tracker": tracker}
    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, state, "ibo", "Bot1",
        "[Bot1] Got an error while launching accounts.", 50.0,
        asyncio.new_event_loop(), deliver=True, ent=None))
    assert client.sent == []          # an incident is already open → no duplicate 🟠
    store.close(); tracker.close()

def test_panel_healthy_resolves_open_incident(tmp_path, monkeypatch):
    # open a panel incident, then a healthy status card → exactly one ✅ Resolved
    ...  # build with _cfg + a real IncidentTracker in state; monkeypatch _alert to capture;
         # call _evaluate_panel with a HEALTHY card; assert one "✅ Resolved" and open_list empty
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3a: Reroute handler.** In `_evaluate_panel`, right after the existing match-search block (the `match_wait = _cant_find_match_minutes(text)` / `_handle_cant_find_match` lines near the top), add:

```python
    if is_panel_silence_selfreport(text):
        return await _handle_panel_selfreport_silence(
            client, cfg, name, target_ref, deliver=deliver, state=state,
            target=target, ent=ent)
```

(Import `is_panel_silence_selfreport` from `watcherdog.classifier` alongside the existing `classify, is_benign_error` import. `_evaluate_panel` has `ent`? It takes `ent` — confirm; it's the panel entity param. If not in scope, pass `None`.)

Add the handler near `_handle_cant_find_match`:

```python
async def _handle_panel_selfreport_silence(client, cfg, name, target_ref, *,
                                           deliver, state, target, ent):
    """The panel posted its own 'has not sent any messages … please check it'
    watchdog notice. That sentence is fresh traffic, so the age-based R6 probe
    never fires for it — handle liveness HERE: /start-probe, then relaunch if the
    app is alive (farm stalled) or report PC-off if it's dead. Returns a handled
    note so monitor_once skips the generic _evaluate_bot alert for this message."""
    now = time.time()
    if not deliver:
        return "dry-run: would probe self-report silence"
    # Debounce a repeat probe within the action window.
    ps = _PANEL_STATE.setdefault(name, panel_rules.PanelState())
    debounce = getattr(cfg, "panel_action_debounce_seconds", 180)
    if ps.last_probe_ts is not None and (now - ps.last_probe_ts) < debounce:
        return "self-report silence: probe debounced"
    ps.last_probe_ts = now
    alive = await _panel_responds(client, target_ref, cfg)
    if alive is None:
        log.warning("[panel] %s self-report silence: probe inconclusive", name)
        return "self-report silence: probe inconclusive"
    if alive is False:
        await _panel_report_pc_off(state, client, target, name, None,
                                   deliver=deliver, cfg=cfg)
        _open_panel_incident(state, name, "self-reported silent + no /start reply", now=now)
        log.info("[panel] %s self-report silence + no /start reply — PC off (HIGH)", name)
        return "self-report silence: PC off"
    # alive: app is up, farm stalled -> relaunch through the existing gate.
    actions = ["select_unfarmed", "start_selected"]
    _open_panel_incident(state, name, "self-reported silent (farm stalled)", now=now)
    if cfg.panel_auto_recover:
        results = await panel_actions.run_sequence(client, target_ref, actions, cfg, confirmed=True)
        ps.last_action_ts = now
        ok = all(r.get("ok") for r in results)
        daily_report.record(cfg.daily_errors_path, panel=name,
                            error="self-reported silence", fix=",".join(actions),
                            result="ok" if ok else "failed")
        log.info("[panel] %s self-report silence: ran %s -> %s", name, actions,
                 "ok" if ok else "failed")
        return f"self-report silence: relaunch {actions} -> {'ok' if ok else 'failed'}"
    posted = await _offer_card(
        state, f"🧰 {name} — panel reported silent; relaunch accounts?",
        buttons.confirm_options([_ACTION_LABELS.get(a, a) for a in actions]),
        panel_target=target_ref)
    ps.last_action_ts = now
    return f"self-report silence: confirm card {actions}" if posted else \
           "self-report silence: alive, no card poster"
```

- [ ] **Step 3b: Unified resolution helper.** Add near `_resolve_bot_incident`:

```python
async def _resolve_incidents_for(state, client, target, bot, now, deliver, cfg, *, announce=True):
    """Close EVERY open incident for a bot (any source) and, if announce, send one
    canonical ✅ Resolved. Inert when tracking is disabled."""
    tracker = state.get("tracker")
    if tracker is None:
        return
    res = tracker.resolve_open_for_bot(bot, "we_fixed_or_healed", now=now)
    if res is None:
        return
    if announce:
        await _alert(state, client, target,
                     format_incident_resolved(bot, res["elapsed"], we_fixed=res["we_fixed"]),
                     deliver, cfg=cfg)
    log.info("RESOLVED %s (%d incident(s), %.0fs, we_fixed=%s)",
             bot, res["count"], res["elapsed"], res["we_fixed"])
```

Repoint the existing `_evaluate_bot` `normal` branch to call `_resolve_incidents_for(...)` instead of `_resolve_bot_incident(...)` (it now covers all sources). Keep `_resolve_bot_incident` only if still referenced; otherwise remove it and its now-dead `we_fixed` local.

- [ ] **Step 3c: Resolve on panel health.** In `_evaluate_panel`, in the `decision.kind == "noop"` healthy branch (where `getattr(decision, "healthy", False)`), after the existing panel Fixed-report logic, add a resolve of any open incident for the panel:

```python
        if getattr(decision, "healthy", False):
            await _resolve_incidents_for(state, client, target, name, now, deliver, cfg)
```

(This is what closes the "silent 2h"/PC-off incidents when the panel is operational again, since the status card classifies `unknown` and would never hit the bot-normal path.)

- [ ] **Step 3d: Resolve on silence recovery.** In `monitor_once`'s `elif not silent and was:` branch, the existing silent `resolve_by_bot("silence", …)` becomes `await _resolve_incidents_for(state, client, target, name, now, deliver, cfg, announce=False)` (the existing `format_recovery_alert` already announces "back online"; keep announce=False to avoid a duplicate ✅).

- [ ] **Step 3e: Channel coordination — suppress duplicate detection 🟠.** In `_evaluate_bot`, at the dedupe gate (right after the `store.last_seen(h)` / `dedupe_window` check, ~line 701-705), add:

```python
    tracker = state.get("tracker")
    if tracker is not None and tracker.open_for_bot is not None:
        if any(tracker.open_list_for_bot(bot)):
            log.info("lifecycle incident already open for %s — suppressing duplicate alert", bot)
            store.record(bot, severity, analysis, h, text, notified=False, ts=now)
            return
```

(Place AFTER the benign/threshold handling and the store dedupe, BEFORE the auto-fix/alert block, so the first detection still opens+alerts but repeats while an incident is open are suppressed.)

- [ ] **Step 3f: Channel coordination — recurring loop.** In `_recurring_loop`, inside the `for g in groups:` loop, skip groups whose bot already has an open incident:

```python
                tracker = state.get("tracker")
                if tracker is not None and any(
                        tracker.open_list_for_bot(b) for b in (g["bots"] or [])):
                    continue
```

- [ ] **Step 3g: Honest wording in the follow-up loop.** In `_incident_followup_tick`, the `giveup` branch, pass `retried=(row["fix_retries"] > 0)`:

```python
            await _alert(state, client, target,
                         format_incident_escalated(
                             bot, row["summary"], elapsed, needs_pc=needs_pc,
                             retried=(row["fix_retries"] > 0)),
                         deliver, cfg=cfg)
```

- [ ] **Step 4: Run focused tests, then the full suite.**
`... -m pytest tests/test_evaluate_panel.py tests/test_mcp_watcher_core.py tests/test_incident_tracker.py tests/test_alerter.py tests/test_classifier.py -q` → green, then `... -m pytest -q` → green.

- [ ] **Step 5: Commit** `feat(monitor): liveness reroute + cross-source resolution + channel coordination`.

---

## Task 5: Full-suite verification

- [ ] Run `... -m pytest -q` → all green.
- [ ] `... python -c "import watcherdog.mcp_watcher, watcherdog.incident_tracker; print('ok')"`.
- [ ] Fix any regression via the same red→green loop; re-run before finishing.

## Self-Review notes
- Spec coverage: A=Task3+4(3a), B=Task1+4(3b/3c/3d), C=Task4(3e/3f), D=Task2+4(3g). All mapped.
- Reuses `_panel_responds`, `panel_actions.run_sequence`, `_offer_card`, `buttons.confirm_options`, `_ACTION_LABELS`, `_open_panel_incident`, `PANEL_AUTO_RECOVER` — no new recovery machinery.
- Every new tracker call guards on `state.get("tracker")` (inert when disabled).
