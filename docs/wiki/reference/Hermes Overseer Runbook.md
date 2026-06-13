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

Run the health probe on a host cron or launchd timer every 1–2 minutes:

```sh
PYTHONPATH= .venv/bin/python scripts/overseer_health.py
```

`PYTHONPATH=` is cleared deliberately so Hermes's own `tools` package does not
shadow this repo's `from tools import …`.

The probe is **socket-free**: it reads the watcher's PID file, beacon timestamp,
incident DB, and log tail directly. It works even when the watcher is completely
down and the overseer socket has not opened yet.

**Exit codes:**
- `0` — healthy, do nothing.
- non-zero — watcher is dead, wedged, or has flagged incidents; wake Hermes.

**JSON fields printed to stdout:**

| Field | Meaning |
|---|---|
| `process_alive` | watcher process is running |
| `beacon_age_s` | seconds since last watcher heartbeat |
| `wedged` | beacon is stale (watcher running but silent) |
| `flagged.count` | number of unresolved flagged incidents |
| `flagged.bots` | list of bot names with open flags |
| `last_sweep` | ISO timestamp of last completed sweep |
| `recent_errors` | last few lines from `data/telegram.err.log` |
| `socket_present` | overseer socket file exists (watcher is up and socket is open) |
| `healthy` | overall boolean summary |

**Cron example (every 2 minutes):**

```cron
*/2 * * * * cd /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring && \
  PYTHONPATH= .venv/bin/python scripts/overseer_health.py > /tmp/watcher_probe.json 2>&1 || \
  hermes chat --provider openai-codex -m gpt-5.3-codex-spark \
    --toolsets terminal,file,vision \
    -q "$(cat /tmp/watcher_probe.json)"
```

The `||` ensures Hermes wakes only on a nonzero exit. Pass the JSON as context
so Hermes knows what is wrong before it calls any overseer endpoint.

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
