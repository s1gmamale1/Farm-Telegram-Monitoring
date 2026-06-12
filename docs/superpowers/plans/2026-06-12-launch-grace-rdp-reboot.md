# Launch-Grace + RDP-Bug Auto-Reboot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This run: INLINE execution by the plan author.**

**Goal:** Stop burning relaunch attempts while a panel is mid-launch (the SF21 false cold-case), recognize the `screen grab failed` RDP-bug marker, and — owner-authorized — auto `Reboot PC → Confirm` after 30 min of persistent RDP-bug, then verify after a 15-min quiet window.

**Architecture:** Three new timers/latches on `PanelState` (`launching_since`, `rdp_bug_since`, `reboot_ts`+`reboot_attempted`); a launch-grace noop between R1 and R2 in `panel_rules.decide`; a 3-state text-bearing probe in `mcp_watcher`; a generic `press_button_then_confirm` in `tg_actions` wrapped as `panel_actions.reboot_pc`; the reboot trigger + post-reboot quiet window + cold-case hold woven into `_evaluate_panel`/the self-report handler.

**Tech Stack:** Python 3.14 stdlib. Venv `.venv/bin/python`. Green-check `pytest $(git ls-files 'tests/*.py')`. Async tests via `asyncio.run`. Worktree `/tmp/wd-rdp`.

---

## Task 1: `panel_rules` — launching state + grace (pure, TDD)

**Files:** Modify `watcherdog/panel_rules.py`; Test `tests/test_panel_rules.py` (append).

Code (complete):

```python
# PanelState additions (dataclass fields, after last_msg_ts):
    launching_since: float | None = None  # status says "launching" since this ts
    rdp_bug_since: float | None = None    # "screen grab failed" first seen (episode)
    reboot_ts: float | None = None        # auto Reboot PC pressed at this ts
    reboot_attempted: bool = False        # once-per-episode latch

# predicate (next to _is_operational):
def _is_launching(status):
    """Panel reports a launch in progress (e.g. 'Accounts launching...') —
    a WAIT state, not a down state (the SF21 lesson: launches take minutes)."""
    return "launching" in ((status.status or "").lower())

# observe() additions (after the over_launch block):
    if _is_launching(status):
        if state.launching_since is None:
            state.launching_since = now
    elif _is_operational(status) or status.status:
        state.launching_since = None

# decide() — insert between the R1 over-launch return and R2:
    # Launch grace: a launch in progress is a WAIT, not a failure. R1 stays
    # above (an over-launched panel mid-launch is still over-launched); R2/R3
    # must not fire while the panel is bringing accounts up within grace.
    grace_s = float(getattr(cfg, "panel_launch_grace_minutes", 15)) * 60.0
    if (state.launching_since is not None
            and (now - state.launching_since) < grace_s):
        return Decision("noop", reason="launch in progress")
```

Tests: launching status → noop within grace; launching past grace → R2 fires; operational clears `launching_since`; R1 (over-launch >15m) still wins while launching.

Mutation-verify: flip `<` to `>=` in the grace check → grace test fails.

---

## Task 2: `tg_actions.press_button_then_confirm` + `panel_menu` text + `panel_actions.reboot_pc`

**Files:** Modify `watcherdog/tg_actions.py`, `watcherdog/panel_actions.py`; Test `tests/test_tg_actions.py` (append; mirror its existing fake-client style).

```python
# panel_menu: add to the success dict:
            "text": (getattr(menu, "message", "") or ""),

# new in tg_actions (after press_button):
_CONFIRM_LABELS = ("confirm", "yes", "✅")


async def press_button_then_confirm(client, chat, button, *, timeout=20.0):
    """Press ``button`` (destructive allowed — the CALLER is the authorization
    gate), then press the confirm button on the panel's follow-up reply.
    Returns {"pressed", "confirmed": bool, "result"} or {"error": ...}."""
    ent = await _resolve(client, chat)
    first = await press_button(client, ent, button, confirmed=True, timeout=timeout)
    if first.get("error"):
        return first
    # The reply to a destructive press usually carries Confirm/Cancel buttons.
    reply = await _latest_with_buttons(client, ent, timeout=timeout)
    if reply is None:
        return {"pressed": first.get("pressed"), "confirmed": False,
                "result": first.get("result", ""),
                "error": "no confirm prompt appeared"}
    for row in (getattr(reply, "buttons", None) or []):
        for btn in row:
            label = (getattr(btn, "text", "") or "").strip().lower()
            if any(c in label for c in _CONFIRM_LABELS):
                await reply.click(text=btn.text)
                final = await _await_reply(client, ent, reply.id, timeout=timeout)
                return {"pressed": first.get("pressed"), "confirmed": True,
                        "result": ((final.message or "") if final else "")[:1500]}
    return {"pressed": first.get("pressed"), "confirmed": False,
            "result": first.get("result", ""),
            "error": "no confirm button on the prompt",
            "buttons": _labels(reply)}


async def _latest_with_buttons(client, ent, *, timeout=20.0, poll=1.0):
    """The panel's newest message that carries inline buttons, within timeout."""
    import asyncio as _asyncio
    deadline = _asyncio.get_event_loop().time() + timeout
    while _asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(ent, limit=3)
        for m in (msgs or []):
            if getattr(m, "buttons", None):
                return m
        await _asyncio.sleep(poll)
    return None

# panel_actions (after restart_panel):
async def reboot_pc(client, panel, cfg):
    """Owner-authorized RDP-bug recovery: Reboot PC -> Confirm (two presses)."""
    return await tg_actions.press_button_then_confirm(client, panel, "reboot pc")
```

Tests with a fake client/menu (mirror existing tg_actions tests): happy two-press (`🔄⚠️ Reboot PC` then a reply with `✅ Confirm` → both clicked, confirmed True); no confirm prompt → confirmed False + error; first press error passthrough.

---

## Task 3: config keys

`config.py` next to `panel_max_attempts`:

```python
        # Launch-grace + RDP-bug auto-reboot (owner-authorized, 2026-06-12).
        self.panel_launch_grace_minutes = float(get("PANEL_LAUNCH_GRACE_MINUTES", "15"))
        self.rdp_bug_reboot_minutes = float(get("RDP_BUG_REBOOT_MINUTES", "30"))
        self.reboot_wait_minutes = float(get("REBOOT_WAIT_MINUTES", "15"))
```

Test in `tests/test_config.py` (defaults 15/30/15, env override works).

---

## Task 4: `mcp_watcher` weave (the big one)

**Files:** Modify `watcherdog/mcp_watcher.py`; Test `tests/test_rdp_reboot.py` (new).

4a. **`_panel_probe`** (replaces `_panel_responds` body; keep `_panel_responds` as a thin `alive`-only wrapper):

```python
_RDP_BUG_RE = re.compile(r"error creating screenshot|screen grab failed", re.I)


async def _panel_probe(client, target_ref, cfg):
    """3-state /start probe that also returns the menu TEXT (so callers can
    read the Status line and spot the screenshot-error marker).
    (True, text) replied; (False, '') no reply -> PC off; (None, '') probe
    itself failed -> inconclusive, never escalate on it."""
    timeout = float(getattr(cfg, "panel_probe_timeout", 15.0))
    try:
        menu = await tg_actions.panel_menu(client, target_ref, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None, ""
    if menu.get("error"):
        return False, ""
    return True, (menu.get("text") or "")


async def _panel_responds(client, target_ref, cfg):
    alive, _text = await _panel_probe(client, target_ref, cfg)
    return alive
```

4b. **RDP-marker ingestion** — helper + two call sites:

```python
def _note_rdp_bug(ps, text, now):
    """Arm/refresh the RDP-bug episode timer from any panel text we read.
    First sight arms it; re-sights do NOT refresh (it measures 'bugged since')."""
    if text and _RDP_BUG_RE.search(text):
        if ps.rdp_bug_since is None:
            ps.rdp_bug_since = now
        return True
    return False
```

Call in `_evaluate_panel` right after `ps` is fetched (on the incoming `text`), and in the self-report handler on the probe text.

4c. **Self-report handler changes** (in `_handle_panel_selfreport_silence`):
- Replace `alive = await _panel_responds(...)` with `alive, probe_text = await _panel_probe(...)`; then `_note_rdp_bug(ps, probe_text, now)`.
- After the alive check, BEFORE the retry-cap/relaunch:

```python
    # Launch grace (the SF21 lesson): the probe says accounts are coming up —
    # wait, don't burn an attempt, don't cold-case mid-launch.
    probe_status = farm_stats.parse_panel_status(probe_text)
    if "launching" in ((probe_status.status or "").lower()):
        if ps.launching_since is None:
            ps.launching_since = now
        grace_s = float(getattr(cfg, "panel_launch_grace_minutes", 15)) * 60.0
        if (now - ps.launching_since) < grace_s:
            log.info("[panel] %s self-report: launch in progress — waiting", name)
            return "self-report: launch in progress"
```

- Retry-cap branch (cold case): add the RDP hold/reboot supersede:

```python
    if ps.recover_attempts >= getattr(cfg, "panel_max_attempts", 3):
        handled = await _maybe_reboot_for_rdp_bug(client, cfg, name, target_ref,
                                                  ps, state, target, deliver, now)
        if handled:
            return handled
        ... (existing cold-case block unchanged)
```

4d. **The reboot step + quiet window** — new helper used by BOTH the self-report retry-cap branch and `_evaluate_panel` (checked right after `_note_rdp_bug`, before the rules `decide`):

```python
async def _maybe_reboot_for_rdp_bug(client, cfg, name, target_ref, ps, state,
                                    target, deliver, now):
    """The owner-authorized RDP-bug ladder rung. Returns a handled-note when it
    acted/held/waited (caller returns it), or None to fall through.

      * post-reboot quiet window -> hold everything
      * rdp bug >= threshold + gates -> press Reboot PC -> Confirm, alert, latch
      * rdp bug set but < threshold -> HOLD the cold case (the reboot path
        supersedes it while the signal is live)
      * no rdp signal -> None (caller proceeds, e.g. to the cold case)
    """
    if ps.reboot_ts is not None:
        wait_s = float(getattr(cfg, "reboot_wait_minutes", 15)) * 60.0
        if (now - ps.reboot_ts) < wait_s:
            return "post-reboot quiet wait"
        return None        # window over: healthy branch resolves, or caller cold-cases
    if ps.rdp_bug_since is None:
        return None
    thresh_s = float(getattr(cfg, "rdp_bug_reboot_minutes", 30)) * 60.0
    if (now - ps.rdp_bug_since) < thresh_s:
        return "rdp-bug hold (reboot pending threshold)"
    if ps.reboot_attempted:
        return None        # one reboot per episode; fall through to cold case
    if not (getattr(cfg, "panel_auto_destructive", False) and deliver):
        return None        # not authorized to press: today's behavior (cold case)
    res = await panel_actions.reboot_pc(client, target_ref, cfg)
    ps.reboot_attempted = True
    ps.reboot_ts = now
    ok = bool(res.get("confirmed"))
    daily_report.record(getattr(cfg, "daily_errors_path", None), panel=name,
                        error="RDP bugged (screen grab failed >=30m)",
                        fix="Reboot PC -> Confirm", result="ok" if ok else "failed")
    await _alert(state, client, target,
                 f"🔄 {name} — RDP bugged ≥{int(thresh_s//60)}m (screen grab failing); "
                 f"pressed Reboot PC{' + Confirm' if ok else ' (no confirm prompt!)'} — "
                 f"re-checking in {int(float(getattr(cfg, 'reboot_wait_minutes', 15)))}m.",
                 deliver, cfg=cfg)
    log.info("[panel] %s RDP-bug reboot pressed (confirmed=%s)", name, ok)
    return "rdp-bug reboot pressed"
```

In `_evaluate_panel`: call it right after `_note_rdp_bug(ps, text, now)`; if it returns a note (acted/held/waiting), return that note (skips rules/flags this sweep). Healthy-branch episode closure additionally resets `rdp_bug_since`, `launching_since`, `reboot_ts`, `reboot_attempted` alongside the existing latch resets.

4e. **Tests** (`tests/test_rdp_reboot.py`, asyncio.run convention, monkeypatched `panel_actions.reboot_pc` / `_alert`, real time math via injected `now`): trigger fires past 30m + gates; once-per-episode; dry-run/flag-off → None (cold case proceeds); <30m → hold note; quiet window holds then expires; marker arms once and never refreshes; probe-text launch grace returns the wait note and burns no attempt (assert `recover_attempts` unchanged).

---

## Task 5: SF21 regression replay + holistic + review + PR

- **Replay test** (in `tests/test_rdp_reboot.py`): drive `_handle_panel_selfreport_silence` with probe replies copied from the real SF21 transcript (`Status: Accounts launching...`, the screen-grab error line) at T+0/T+4/T+8 min → assert `recover_attempts == 0`, no cold case, `rdp_bug_since` armed at the error sight; then an operational status → healthy reset.
- Full green-check; mutation spot-checks per task; reviewer pass (full — destructive path); push → PR → merge-if-clean; restart the live watcher.

## Self-Review (plan author)

Spec C1→T1+T4c; C2→T4b; C3→T2+T4d; plumbing→T4a+T2; config→T3; testing incl. replay→T4e+T5. Signatures consistent: `_panel_probe -> (alive, text)`; `_maybe_reboot_for_rdp_bug(...) -> str|None`; `reboot_pc(client, panel, cfg)`; `press_button_then_confirm(client, chat, button, *, timeout)`. `re` already imported in mcp_watcher; `farm_stats` already imported. No placeholders.
