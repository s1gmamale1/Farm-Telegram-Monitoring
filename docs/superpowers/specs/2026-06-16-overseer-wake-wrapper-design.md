# Overseer Wake Wrapper — Design

**Date:** 2026-06-16
**Goal:** Hardened, host-side **on-trouble** wake for the external monitoring agent
(Sherlock/Hermes): run the health probe on a timer, and invoke a *pluggable* agent
command **only when the deterministic core has genuinely failed** — with
single-flight, cooldown, and a wake log — without ever touching Hermes's own setup.

## Boundary (confirmed with the Hermes manager, "Frostie")

- **WatcherDog repo owns the TRIGGER only:** `scripts/overseer_health.py` (probe),
  `scripts/overseer_wake.py` (wrapper), the launchd timer plist, and the wake
  log/lock/cooldown state. Deterministic, testable, no AI.
- **The Hermes manager owns the INVOCATION command** (`OVERSEER_WAKE_CMD`). Sherlock
  stays a CLI **worker profile** — no Telegram token, no gateway bot. `hermes chat`
  has no `--stdin`, so the manager supplies a shim
  (e.g. `/Users/aisigma/Sigma-Agents/watcherdog/sherlock-wake-cmd.sh`) that adapts our
  contract to a `sherlock …` call. **We never edit `~/.hermes/`.**

### The invocation contract (locked)
The wrapper invokes `$OVERSEER_WAKE_CMD` as: **`argv[1]` = a one-line reason**,
**stdin = the probe JSON**. Exit code is logged, not acted on. If `OVERSEER_WAKE_CMD`
is unset/empty, the wrapper logs `would-wake` and no-ops (safe to install + verify the
whole trigger before Sherlock is plugged in).

## Wake policy (decided)

- **On-trouble only** (no heartbeat).
- **Wake only on STUCK trouble:** watcher dead/wedged trips immediately; a flagged
  incident wakes the agent only after it has been open longer than the core's recovery
  window, so the wrapper never wakes during routine 5–10 min auto-recovery.

---

## Component A — Probe exit-code gating (`scripts/overseer_health.py`)

**Change.** Add `OVERSEER_STUCK_MIN` (default **12** min — just above the ~10-min
ladder window). In `build_report`, compute `flagged_stuck` = open novel incidents whose
oldest `opened_ts` is older than `STUCK_MIN`. The **exit code** becomes:

```
unhealthy = (not alive) or beacon_stale or (flagged_stuck.count > 0)
```

- Dead/wedged still trip immediately (unchanged).
- A flagged incident younger than `STUCK_MIN` (core is laddering it) → **exit 0**.
- The JSON **still reports the full `flagged` and `needs_human` exactly as today**
  (visibility unchanged) and **adds `flagged_stuck` = {count, bots, stale:[{bot,open_min}]}**
  for transparency. `healthy` in the JSON tracks the new `unhealthy` (so it stays
  consistent with the exit code).

**Where the ages come from.** `_flagged` already reads open novel incidents; extend it
(or add `_flagged_stuck`) to carry each bot's oldest `opened_ts` so the threshold can be
applied. `IncidentTracker.novel_list()` rows include `opened_ts`.

**Back-compat note.** This changes when the probe exits non-zero. The runbook §2 and
`AGENTS.md` probe-field list must be updated to describe `flagged_stuck` and the
"stuck-gated wake."

**Tests.** flagged incident age < `STUCK_MIN` → exit 0 but still in JSON `flagged`;
age > `STUCK_MIN` → exit 1 + present in `flagged_stuck`; dead/wedged → exit 1 regardless
of incident age; `flagged_stuck` shape; `needs_human`/`escalated_recent` still
report-only and never affect the exit code.

---

## Component B — The wake wrapper (`scripts/overseer_wake.py`) — core deliverable

A small Python script the timer runs every ~120 s. Pure decision logic + one thin
subprocess invoke. Loads `cfg` via `load_config()` and reuses
`overseer_health.build_report` **in-process** (no JSON re-parse).

**Algorithm:**
```
report, code = overseer_health.build_report(cfg, now)
if code == 0:                          log "ok"          ; exit 0    # healthy / in-flight
if _lock_held_by_live_pid():           log "skip:inflight"; exit 0    # single-flight
reason = _reason(report)               # "watcher down" | "wedged" | "stuck: SF14,SF7"
urgent = (not report["process_alive"]) or report["wedged"]
if (not urgent) and _within_cooldown(now):
                                       log "skip:cooldown"; exit 0    # stuck-only cooldown
cmd = env OVERSEER_WAKE_CMD
if not cmd:                            log "would-wake "+reason; exit 0   # no-op until plugged in
with _lock(pid):                       # held for the agent's lifetime
    _stamp_wake(now)
    rc = run(cmd, argv1=reason, stdin=json.dumps(report), timeout=TIMEOUT)
    log f"woke rc={rc} reason={reason}"   # on timeout: kill, log "woke timeout", release
```

**Why the timeout matters:** the lock is held while the agent runs, so a **hung agent
would hold the lock forever and permanently disable all future wakes.**
`OVERSEER_WAKE_TIMEOUT_MIN` (default **15**) caps the invocation — on timeout the wrapper
kills the command, logs `woke timeout`, and releases the lock so the next tick can wake
again.

**State (under `data/`, all size-bounded):**
- `overseer_wake.lock` — single-flight; holds the wrapper PID; **stale lock (PID not
  alive via `kill -0`) is reclaimed**.
- `overseer_wake.last` — last-wake epoch for cooldown.
- `overseer_wake.log` — one line per decision (ts, decision, reason, bots), **truncated
  to its last ~1 MB** when it exceeds that (we learned the 122 MB lesson).

**Knobs (env, with defaults):**
- `OVERSEER_WAKE_CMD` — the manager's command (unset ⇒ no-op).
- `OVERSEER_WAKE_COOLDOWN_MIN` — default **30**. Applies to *stuck-incident* wakes only.
- `OVERSEER_WAKE_TIMEOUT_MIN` — default **15**. Max agent runtime before kill + lock release.
- Dead/wedged **bypass cooldown** (single-flight still applies) — get the watcher back ASAP.

**Error handling (fail toward waking, never crash the timer):**
- `build_report` raises → treat as trouble, attempt a wake (`reason="probe error"`).
- `OVERSEER_WAKE_CMD` missing/non-zero/not-found → log it, release lock, exit 0.
- Lock always released (context manager / `finally`).

**Tests (pure logic; mocked clock + invoke; real lock/stamp/log files on `tmp_path`):**
healthy → no wake; dead → wake (bypasses cooldown); wedged → wake; stuck flagged → wake;
in-flight flagged (code 0) → no wake; lock held by live PID → skip; lock held by dead PID
→ reclaim + wake; within cooldown (stuck) → skip; dead within cooldown → still wakes;
`OVERSEER_WAKE_CMD` unset → would-wake no-op (no invoke); invoke receives `argv[1]`=reason
and stdin=JSON; invoke failure → logged, lock released; **agent exceeds
`OVERSEER_WAKE_TIMEOUT_MIN` → killed, `woke timeout` logged, lock released (next tick can
wake)**; wake log truncates at the cap.

---

## Component C — Install (`com.watcherdog.overseer-wake.plist` + installer/doc)

launchd timer (`StartInterval 120`) running `overseer_wake.py` from the repo with the
repo `.venv`. launchd over cron on macOS (survives logout, explicit env, no PATH
surprises). The plist's env block carries `OVERSEER_WAKE_CMD` (+ optional knobs); a short
installer/doc covers `launchctl load`, where to set `OVERSEER_WAKE_CMD`, and how to read
the wake log. Documented contract restated for the manager.

---

## Component D — Foundation (OPTIONAL, documented runbook step — not code)

Install the **watcher itself under launchd `KeepAlive`** (plist already fixed in PR #21)
so a dead watcher self-heals in seconds with **no agent wake needed** — fixing the gap we
hit (watcher silently dead ~1.5 h after a terminal-tied launch died on SIGHUP). With this,
the wake-wrapper only handles *wedged* + *stuck incidents*; *process-dead* is launchd's job.

This is a **session-bound one-time migration the owner runs** (not auto-run): **stop the
nohup watcher first, then `launchctl load`** — never two processes on
`data/watcher.session` at once (corruption rule). Documented in the runbook as an optional
step; A+B+C work with or without it (the wrapper still detects + wakes on a dead watcher).

---

## Files

- Modify: `scripts/overseer_health.py` (stuck gating + `flagged_stuck`).
- Create: `scripts/overseer_wake.py` (the wrapper).
- Create: `com.watcherdog.overseer-wake.plist` (launchd timer).
- Create/extend: a short install doc + runbook §2 update (`docs/wiki/reference/Hermes Overseer Runbook.md`), `AGENTS.md` probe-field note.
- Tests: `tests/test_overseer_wake.py` (new), extend `tests/test_overseer_health.py`.

## Out of scope (YAGNI)
- No heartbeat wake (on-trouble only, by decision).
- No changes to Hermes/Sherlock config, the shim, or `~/.hermes/` (manager-owned).
- No JSONL event stream (still deferred from the earlier ergonomics work).
