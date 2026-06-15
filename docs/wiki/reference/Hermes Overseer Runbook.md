# Hermes Overseer Runbook

The overseer surface lets an external AI agent (Hermes) observe and act on the
watcher without needing a live Telegram session. This runbook covers Option B:
**wake-on-trouble** — Hermes wakes only when the probe says something is wrong,
staying silent otherwise.

---

## 1. Keep the Watcher Alive (launchd)

Install the service so launchd restarts the watcher automatically on crash or
reboot:

```sh
cp com.watcherdog.telegram.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.watcherdog.telegram.plist
```

`KeepAlive` and `RunAtLoad` are both `true` in the plist — launchd will
resurrect the process if it exits for any reason. This is the foundation of the
wake-on-trouble loop: Hermes can restart the watcher via launchd and be
confident it stays up.

To stop or uninstall:

```sh
launchctl unload ~/Library/LaunchAgents/com.watcherdog.telegram.plist
```

Logs land in `data/telegram.out.log` and `data/telegram.err.log` inside the
repo.

---

## 2. Wake-on-Trouble (Option B)

A host-side **launchd timer** runs the hardened wake-wrapper
`scripts/overseer_wake.py` every ~120 s. The wrapper runs the deterministic
health probe **in-process** (`overseer_health.build_report` — no JSON re-parse)
and invokes a *pluggable* agent command **only when the core has genuinely
failed**, with single-flight, a keyed/stuck-only cooldown, a process-group kill
on timeout, and a size-bounded wake log. WatcherDog owns the **trigger** only;
the Hermes manager owns the **invocation command** (`OVERSEER_WAKE_CMD`).

> **This replaces the old raw `… || hermes chat …` cron one-liner.** That form
> had no single-flight (overlapping ticks could double-wake), no cooldown (it
> re-woke every 2 min while an incident stayed open), no timeout (a hung agent
> ran forever), and woke on *every* flagged incident — including ones the core
> was still mid-recovery on. The wrapper fixes all four.

### The probe (`scripts/overseer_health.py`)

The wrapper calls it in-process, but you can run it standalone for a one-shot:

```sh
PYTHONPATH= .venv/bin/python scripts/overseer_health.py
```

`PYTHONPATH=` is cleared deliberately so Hermes's own `tools` package does not
shadow this repo's `from tools import …`.

The probe is **socket-free**: it reads the watcher's PID file, beacon timestamp,
incident DB, and log tail directly. It works even when the watcher is completely
down and the overseer socket has not opened yet.

**Exit codes:**
- `0` — healthy (or a flagged incident is still **in-flight**, younger than
  `OVERSEER_STUCK_MIN`, so the core is mid-recovery): do nothing.
- non-zero — watcher is dead, wedged, or has a **stuck** flagged incident (open
  past `OVERSEER_STUCK_MIN`): a wake-trigger holds.

> **Stuck-gated wake (changed behavior):** the exit code now gates on
> `flagged_stuck`, **not** the full `flagged` set. A freshly-flagged incident the
> core is laddering (kill → select → start, ~5–10 min) stays **exit 0** and does
> NOT wake the agent; only one open longer than `OVERSEER_STUCK_MIN` (or whose
> age is unprovable) trips a wake. `flagged` still reports the full open set for
> visibility.

**JSON fields printed to stdout:**

| Field | Meaning |
|---|---|
| `process_alive` | watcher process is running |
| `beacon_age_s` | seconds since last watcher heartbeat |
| `wedged` | beacon is stale (watcher running but silent) |
| `flagged.count` | number of unresolved flagged incidents (full open set, report-only) |
| `flagged.bots` | list of bot names with open flags (full open set, report-only) |
| `flagged_stuck` | `{count, bots, stale:[{bot, open_min\|null, reason?}]}` — the subset of `flagged` open longer than `OVERSEER_STUCK_MIN` (default 12 min). **This is what GATES the exit code.** `open_min:null` + `reason:"age_unknown"` when `opened_ts` is missing/unparseable (fail toward waking). |
| `escalated_recent` | `{count, bots}` — panels that escalated (auto-recovery gave up → human alerted) in the last 24h. **Report-only**: a fading 24h window, never flips the exit code. |
| `needs_human` | `{count, bots, stale:[{bot, parked_h}]}` — panels parked because the PC is off (only a human power-on fixes them). **Report-only and human-owned**: NO fade (a still-off PC stays visible forever) and it NEVER flips `healthy`, so Hermes is not woken in a loop on it. |
| `last_sweep` | newest sweep line as `"HH:MM (N chats, M healthy)"` (parsed from `data/gui_run.log`), NOT an ISO timestamp |
| `recent_errors` | newest-first error/traceback lines across BOTH `data/telegram.err.log` AND `data/gui_run.log`, deduped + bounded |
| `socket_present` | overseer socket file exists (watcher is up and socket is open) |
| `healthy` | overall boolean summary |

`get_stats` now also returns, per fleet entry, an `incident` (`"open"` / `"parked"` / `null`) and `down_since_h` (hours since that incident began) — so one `get_stats` call is the whole fleet board.

### The wake-wrapper (`scripts/overseer_wake.py`)

The launchd timer runs the wrapper, not the bare probe. Each tick, the wrapper:

1. Runs `build_report` in-process. **Healthy / in-flight (probe exit 0) → log
   `ok`, exit.**
2. **Single-flight:** if a previous wake is still running (lock held by a live
   PID) → log `skip:inflight`, exit. A *stale* lock (PID dead) is reclaimed.
3. **Keyed, stuck-only cooldown:** if this incident's key was woken within
   `OVERSEER_WAKE_COOLDOWN_MIN` → log `skip:cooldown`, exit. The key is the
   sorted stuck-bot set (e.g. `stuck:SinFermera7,SinFermera14`), so a **new,
   unrelated stuck bot has a different key and is never suppressed** by another
   bot's cooldown. **Dead/wedged are urgent and bypass cooldown entirely**
   (single-flight still applies) — get the watcher back ASAP.
4. **`OVERSEER_WAKE_CMD` unset/empty → log `would-wake <reason>`, no-op.** Safe
   to install and verify the whole trigger before Sherlock is plugged in.
5. Otherwise: acquire the lock atomically, stamp the cooldown, and invoke the
   command. The command's exit code is **logged, never acted on**.

### Install the launchd timer

launchd over cron on macOS: survives logout, explicit env, no PATH surprises.
The plist is a **periodic one-shot** (`StartInterval 120`, **no `KeepAlive`** —
it runs, decides, exits, and launchd re-runs it on the timer).

```sh
cp com.watcherdog.overseer-wake.plist ~/Library/LaunchAgents/
launchctl load     ~/Library/LaunchAgents/com.watcherdog.overseer-wake.plist
launchctl kickstart gui/$(id -u)/com.watcherdog.overseer-wake   # run once now to verify
```

To stop / uninstall:

```sh
launchctl unload ~/Library/LaunchAgents/com.watcherdog.overseer-wake.plist
```

### The `OVERSEER_WAKE_CMD` contract (locked — manager-owned)

`OVERSEER_WAKE_CMD` is **a shlex-splittable command string OR an executable
path** (NOT a shell string — the wrapper runs it with `shell=False` to avoid
quoting ambiguity in the plist). The wrapper:

- `shlex.split`s it and **appends the one-line reason as the final argv
  element** (e.g. `… "stuck: SinFermera7"`),
- feeds the **probe JSON on stdin**,
- logs the command's exit code (**never acts on it**).

If `OVERSEER_WAKE_CMD` is **unset/empty the wrapper logs `would-wake` and
no-ops** — install and verify the trigger before any agent is wired in.

**Where to set it:** either in the plist's `EnvironmentVariables` block
(uncomment the `OVERSEER_WAKE_CMD` key) **or** in the launchctl environment:

```sh
launchctl setenv OVERSEER_WAKE_CMD /Users/aisigma/Sigma-Agents/watcherdog/sherlock-wake-cmd.sh
```

The manager points it at a shim (example path
`/Users/aisigma/Sigma-Agents/watcherdog/sherlock-wake-cmd.sh`) that reads
`argv[-1]`=reason + stdin=JSON and calls `sherlock …`. **We never edit
`~/.hermes/` or the shim** — that surface is manager-owned.

### Knobs (env, with defaults)

| Knob | Default | Effect |
|---|---|---|
| `OVERSEER_WAKE_CMD` | *(unset)* | The manager's command. Unset ⇒ `would-wake` no-op. |
| `OVERSEER_STUCK_MIN` | `12` | Minutes a flagged incident may stay open before it's **stuck** and gates the probe exit code (read by the probe). |
| `OVERSEER_WAKE_COOLDOWN_MIN` | `30` | Per-key cooldown between **stuck-incident** wakes. Dead/wedged bypass it. |
| `OVERSEER_WAKE_TIMEOUT_MIN` | `15` | Max agent runtime before the wrapper kills the whole process group (SIGTERM→SIGKILL) and releases the lock. |

**Why the timeout matters:** the single-flight lock is held for the agent's
whole lifetime, so a **hung agent would hold the lock forever and permanently
disable all future wakes.** On timeout the wrapper kills the agent's entire
process **group** (`start_new_session=True` + `killpg` — no orphaned shim/agent
children survive), logs `woke timeout`, and releases the lock so the next tick
can wake again.

### Where to read the wake log

Every decision appends one compact line to **`data/overseer_wake.log`**
(`ts decision reason bots rc elapsed_s`). The file is **truncated to its last
~1 MB** when it exceeds that (we learned the 122 MB lesson). launchd's own
stdout/stderr go to `data/overseer-wake.out.log` / `data/overseer-wake.err.log`.

```sh
tail -f data/overseer_wake.log
```

Decisions you'll see: `ok`, `skip:inflight`, `skip:cooldown`, `would-wake`,
`woke` (with `rc=` from the agent), `woke timeout`, and `error` (an unexpected
wrapper fault — logged; the timer still exits 0 so it never stops re-running).

---

## 2a. (Optional) Watcher under launchd `KeepAlive` — owner migration

This is the **Component D foundation**: run the watcher *itself* under launchd
`KeepAlive` (the plist in §1) so a **process-dead** watcher self-heals in seconds
with **no agent wake needed** — closing the gap where a terminal-tied launch
died on `SIGHUP` and sat silently dead ~1.5 h. With this in place the wake-wrapper
only has to handle *wedged* + *stuck incidents*; *process-dead* becomes launchd's
job. A+B+C work **with or without** it (the wrapper still detects and wakes on a
dead watcher), so this step is optional.

It is a **one-time migration the OWNER runs by hand** (not auto-run), because the
live watcher today is a manual `nohup` process. **Never run two processes on
`data/watcher.session` at once** (session corruption). Migrate in this order:

```sh
# 1. STOP the old nohup watcher FIRST (find its PID, SIGTERM — SIGINT is ignored).
pkill -TERM -f '[Pp]ython.*run_watcher\.py'      # or: kill -TERM <pid>

# 2. Load the KeepAlive plist (it will start the watcher).
launchctl load ~/Library/LaunchAgents/com.watcherdog.telegram.plist

# 3. VERIFY exactly ONE watcher process is running (never two on the session).
pgrep -fl '[Pp]ython.*run_watcher\.py'           # expect a single line
```

If step 3 shows two processes, stop one immediately — overlapping writers corrupt
`data/watcher.session`.

---

## 3. Strict Overseer Prompt

Copy this block verbatim as the Hermes system/task prompt:

```
You are the WatcherDog overseer. You have been woken because the health probe
returned a nonzero exit. The probe JSON is provided as context.

Rules — follow them strictly:

1. DIAGNOSE before acting. Read probe fields and call list_flagged /
   read_bot / get_stats before touching anything.

2. Use the overseer API for all actions:
     scripts/overseer_cli.py <method> '<json-params>'
   Available methods: list_flagged, read_bot, list_buttons, get_stats,
   screenshot, resolve_flagged, teach_fix, press_button, run_ladder.
   Do NOT poke Telegram directly.

3. Do NOT press destructive buttons (Kill all, Reboot PC, etc.) and do NOT
   call run_ladder unless OVERSEER_ALLOW_DESTRUCTIVE=true in the environment.
   When the flag is off, observe + teach_fix + resolve_flagged only.

4. If a code edit is needed:
   a. Edit the file.
   b. Run: .venv/bin/python -m py_compile <file>
   c. Run: .venv/bin/pytest tests/<relevant>.py -q
   d. Run: .venv/bin/python run_watcher.py --once --dry-run --verbose
   e. Then restart (see step 5).

5. If the watcher is down:
   a. Inspect data/telegram.err.log and data/gui_run.log for the root cause.
   b. Fix the cause if it is safe and clear.
   c. Restart: launchctl kickstart -k gui/$(id -u)/com.watcherdog.telegram
      (launchd KeepAlive will keep it up after that.)

6. REPORT only when you took action or a human is needed. Stay silent / no-op
   when the watcher is healthy and no flags are open.
```

---

## 4. Environment Variables

```sh
OVERSEER_SOCKET=data/overseer.sock     # enables the socket surface (unset = off)
OVERSEER_TOKEN=<long random>           # optional shared secret for all requests
OVERSEER_ALLOW_DESTRUCTIVE=false       # flip true to let Hermes press Kill/Reboot + run_ladder
```

Set these in `.env` alongside the other watcher vars.

**Important:** always clear `PYTHONPATH` when invoking Hermes one-shots against
this repo. Hermes ships its own `tools` package; if `PYTHONPATH` includes its
install prefix, imports like `from tools import press_button` resolve to the
wrong module.

---

## 5. Endpoint Gating

| Endpoint | Gated by `OVERSEER_ALLOW_DESTRUCTIVE`? |
|---|---|
| `list_flagged` | No — always available |
| `read_bot` | No — always available |
| `list_buttons` | No — always available |
| `get_stats` | No — always available |
| `screenshot` | No — always available |
| `resolve_flagged` | No — always available |
| `teach_fix` | No — already refuses `auto:yes` + destructive intent |
| `press_button` (non-destructive label) | No — always available |
| `press_button` (destructive matched label) | **Yes — refused when `false`** |
| `run_ladder` | **Yes — refused when `false`** |

---

## 6. Safety Note

`OVERSEER_ALLOW_DESTRUCTIVE` governs **only the socket-driven overseer path**.

The in-process core still auto-recovers per `PANEL_AUTO_DESTRUCTIVE` (default
`true`), including the owner-authorized RDP auto-reboot (which uses
`press_button_then_confirm`, bypassing this flag entirely). This means panels
can still be auto-rebooted by the watcher core while `OVERSEER_ALLOW_DESTRUCTIVE`
is `false` — that is intentional: the core's recovery loop is deterministic and
owner-approved; the overseer flag only guards the external AI agent path.
