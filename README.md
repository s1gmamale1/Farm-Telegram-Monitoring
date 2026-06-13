# WatcherDogBot 🐶

A monitor for a fleet of **Telegram farm bots**. It watches the 24 `SinFermera*`
CS2/Steam drop-farming bots (the **Farms** folder), **auto-fixes the errors it
already knows how to fix with zero AI cost**, escalates only genuinely *novel*
errors to an LLM (once — then it remembers), answers your questions, drives the
farm control panels on command, and posts an hourly health report.

```
                    ┌──────────── ONE process, TWO Telegram logins ────────────┐
                    │                                                          │
  Farms folder ───▶ │  USER account  (Sigma Male @s1gmamale1)                  │
  (24 SinFermera    │   • sweeps the folder every ~2 min                       │
   farm bots)       │   • reads each bot (Bot API can't read other bots)       │
                    │   • drives panels / presses inline buttons               │
                    │                                                          │
  you / the group ─▶│  BOT  (@sherlock_homeless_chigga_bot)                    │
                    │   • the human-facing talker: commands + Q&A              │
                    │   • DMs you proactive alerts                             │
                    └──────────────────────────────────────────────────────────┘
                                          │
   each farm message ─▶ script-first router (no LLM) ─▶ AI only if novel ─▶ you
                                          │
                                          └──▶ SQLite history + learned-fixes "brain"
```

> **TL;DR** — the current, supported entry point is **`run_watcher.py`** (MTProto
> / Telethon). The old screenshot-and-OCR GUI mode (`run_gui.py`) and the
> log-file tailer (`run.py`) are **legacy** and documented in the
> [Appendix](#appendix-legacy-modes) at the bottom.

**Docs map**
| File | What it covers |
|------|----------------|
| **README.md** (this file) | What it is, how it's wired, quick start, configuration. |
| [HOWTORUN.md](HOWTORUN.md) | The operational runbook — install, run, watch, stop, troubleshoot. |
| [DOCUMENTATION.md](DOCUMENTATION.md) | Developer internals — module-by-module, data/state, debugging. |
| [docs/OPTIMIZATION_PLAN.md](docs/OPTIMIZATION_PLAN.md) | Why/how the watcher became *script-first, AI-last*. |
| [docs/hermes/](docs/hermes/) | The agent's own operating guides + the panel "skills" (00–07). |
| [docs/wiki/](docs/wiki/) | **Obsidian knowledge base** — interlinked notes + a canvas map. Open the vault and start at `Home`. |

---

## 1. The big idea: two identities, one process

The Telegram **Bot API forbids a bot from reading another bot's messages** — so a
bot alone can never watch the `SinFermera*` farm bots. WatcherDog solves this by
running **two logins on one asyncio event loop** (`run_watcher.py` →
`watcherdog/mcp_watcher.run`), each with a clean job:

| | **The USER account** — *the watcher* | **The BOT** — *the talker* |
|---|---|---|
| Identity | `Sigma Male` (`@s1gmamale1`) | `@sherlock_homeless_chigga_bot` |
| API | MTProto (Telethon) | Bot API (over Telethon) |
| Reads the farm bots? | **Yes** — only a user account can | No (forbidden by Telegram) |
| Talks to humans? | No (it never speaks in groups) | **Yes** — commands, Q&A, alert DMs |
| Can act on panels? | **Yes** — presses inline buttons | Only by relaying to the user account |

The user account *owns* the bot (it created it). For the bot to DM you alerts,
each owner in the allow-list (`ALLOWLIST`) must press **Start** on the bot
**once**; otherwise alerts quietly fall back to being sent by the user account.

> **Heads up:** that bot token was reclaimed from an old *OpenClaw* deployment.
> If the bot ever behaves oddly or "fights" over updates, suspect a leftover
> OpenClaw process still polling the same token.

---

## 2. The big idea: script-first, AI-last

Every message a farm bot posts hits a **deterministic router**
(`watcherdog/auto_fix.py`, wired into `mcp_watcher._evaluate_bot`). Known
situations are handled by scripts at **zero token cost**; a genuinely *novel*
error gets the deterministic generic-restart ladder (`novel_recovery.py`) and
is flagged for the optional overseer. **The monitor loop never calls a model
in any mode** (AI-removal track, PRs #11–#17): triage, reports, recovery and
the weekly digest are all script-only. The model is invoked **only** when you
ask something free-form — and all other AI lives behind the opt-in overseer
endpoint surface (`OVERSEER_SOCKET`, see
`docs/wiki/reference/Overseer Endpoints.md`).

```
farm message ─▶ classify()                          (scripts, no LLM)
                  │
         normal ──┴── error / unknown
                         │
                  learned_fixes.find_fix(text)       (scripts, no LLM — the "brain")
                         │
             hit ────────┴──────── miss
              │                     │
   known, non-destructive      NOVEL error
   → APPLY the mapped          → ask the AI ONCE: it proposes + applies a fix,
     button steps now            then save_fix() with a runnable `action`
              │                   so the *next* time is script-only
              └──────────┬────────┘
                         ▼
                 daily_report.record()  (log every fix)
```

- **Known error** → the router executes the saved button steps (e.g. *Kill All →
  Start 4 accounts*) and reports what it did, past tense. No question asked.
- **Novel error** → the deterministic generic-restart ladder
  (`novel_recovery.py`) runs **once** (ban/captcha family exempt — those ping
  you immediately), the incident is flagged for the optional overseer, and
  retries are paced by the incident follow-up loop. **No model.** The overseer
  (when connected) can `teach_fix` an executable `action` so every repeat is
  handled by the router script-only.
- **Destructive panel recovery** (the deterministic Kill-all → select-4 → Start
  ladder) runs **autonomously by default** (`PANEL_AUTO_DESTRUCTIVE=true`). The
  inline confirm button is the **opt-in**: set `PANEL_AUTO_DESTRUCTIVE=false` and
  destructive steps post a one-tap card instead of running themselves.
- **Launch grace** — `Accounts launching…` is a WAIT state: no relaunch presses
  and no cold-casing for up to `PANEL_LAUNCH_GRACE_MINUTES` while accounts come
  up (launches take minutes per batch).
- **RDP-bug auto-reboot** — the panel's own `Error creating screenshot: screen
  grab failed` line persisting `RDP_BUG_REBOOT_MINUTES` with the panel still not
  operational presses **Reboot PC → Confirm** once per episode (a fresh `/start`
  probe right before the press stands down if the panel is actually live), then
  verifies after `REBOOT_WAIT_MINUTES`. Gated by `PANEL_AUTO_DESTRUCTIVE`.
- **`type: human` fix** → the router does *not* act; it pings you (a person is
  needed).

The knowledge base is a plain-Markdown file you can read and edit:
`data/hermes/learned_fixes.md` (`watcherdog/learned_fixes.py`).

---

## 3. What you actually see

- **Proactive alerts** — when a bot errors, gets banned, hits captcha/Steam
  Guard, or goes **silent** past `SILENCE_THRESHOLD_MINUTES`, **everyone in the
  `ALLOWLIST`** gets a DM (and a "✅ back online" note when it recovers).
  De-duplicated so the same bug doesn't spam you within `DEDUPE_WINDOW`.
- **Autonomous panel recovery + one-line outcome** — the deterministic engine
  (`watcherdog/panel_rules.py`, driven by `mcp_watcher._evaluate_panel`) watches
  each panel's status and, by default, **fixes it itself** (Kill all → select 4 →
  Start, no tap), then posts **one concise line** per outcome:
  ```
  SinFermera9 | over-launch | Fixed ✅
  SinFermera3 | silent 71m (dead) | NOT fixed ❌ → needs PC
  ```
  A cold case (frozen RDP / black screen / total silence) it *can't* fix from
  Telegram is reported as `NOT fixed ❌ → needs PC` — the host-side fix lives in a
  separate per-PC tool (see the Appendix note at the bottom).
- **Inline confirm/action buttons** (`watcherdog/buttons.py`) — when destructive
  auto-run is *off* (`PANEL_AUTO_DESTRUCTIVE=false`), or for the bot's own
  relaunch flow, a yes shows tappable buttons instead of a typed question:
  ```
  ⚠️ SinFermera9 — farm dead (device on). Proposed: relaunch (Kill All → Start 4).
  [ ✅ Do it ]   [ ✋ Skip ]   [ 🔁 Restart instead ]
  ```
  Each button is a **signed, single-use, expiring token** — it only ever runs the
  exact action it was posted for. By the owner's deliberate choice, **anyone in
  the group can tap** (the token is the authorization, not the user id); the
  presser is logged. Re-tighten with `BOT_ACTION_USERS` if the group is untrusted.
- **Hourly farm report** — a layered, status-grouped report posted to the **Class A
  Farming** forum topic every hour: a *needs-attention* triage block first (each
  🔴 panel one line with its reason + the watcher's last action, e.g.
  `accounts 2/4 · 24m · relaunch ×2` or `cold-cased, needs PC`; ⚠️ quiet panels
  compacted), then the ✅ farming roster, then what changed since the last report
  (`🆕` newly-flagged, `recovered since HH:MM`) and an `⏰ gap` notice if reports
  were skipped. Collapses to a one-liner when all panels are farming, and ends with
  the *"🔧 Fixed last hour"* line. No PC map needed — grouping is by status.
- **Fast slash-commands** (no LLM, instant, free) — `/status`, `/problems`,
  `/silent`, `/fixes`, `/mode`. AI-backed commands (`/weekly`, `/today`, `/top`,
  `/worst`, `/value`, `/check N`, `/bans`, `/compare`, `/whatsnew`) read the
  folder and summarize only when `DISABLE_AI=false`. `/help` lists everything.
- **Live progress + resume** — an action task replies instantly with a status
  message that **edits live** as each step runs, then is replaced by the final
  answer. Tasks are persisted, so a restart mid-task **resumes** it. The bot is
  **multitasking**: read-only questions answer in parallel while a long action
  runs.

---

## 4. Admin powers (gated)

Most people are answered **read-only**. Authorized/admin users get more:

- **Drive panels** (`BOT_ACTIONS_ENABLED`, `BOT_ACTION_USERS`) — *"press Drop
  Stats on panel 3"*, *"stop all panels"*; destructive buttons still confirm.
- **Grant/revoke access** (`BOT_ADMIN_USERS`) — *"give @someone access"* → the
  agent calls `grant_bot_access` itself; persisted to `data/bot_access.json`.
- **Self-edit** (`BOT_SELF_EDIT_ENABLED`) — an admin can tell the bot to change
  WatcherDog's **own source**. It rewrites the whole file in one pass, **syntax-
  checks** it, and writes only if valid, keeping a `<file>.bak.<timestamp>` undo.
- **Self-restart** (`BOT_SELF_RESTART_ENABLED`) — after a self-edit the bot can
  relaunch itself. It validates that the whole project still imports first
  (rolling back if not), and a detached supervisor restores the backups and
  relaunches if the new code never comes up healthy.

These are triggered **only by a real authorized sender**, never by message *text*
(prompt-injection guard). ⚠️ Self-editing modifies running code — the `.bak.*`
files are your undo.

---

## 5. Quick start

```bash
cd ~/Documents/WatcherDogBot

# 0a. (optional) Check the MTProto handshake without logging in (sends nothing
#     to your phone — just connect + is-authorized):
.venv/bin/python tools/tg_probe.py

# 0b. One-time: authorize the user-account session (phone + login code).
#     If this machine has a stale/unauthorized session or no code arrives, use
#     the original Telethon-managed login flow and move the stale file aside:
.venv/bin/python tools/tg_login.py --reset-session --legacy-start

# 1. (Optional) Ollama is only needed if you flip DISABLE_AI=false for the
#    legacy model triage — the default runtime is fully model-free.
# ollama list

# 2. Run it (foreground, verbose)
.venv/bin/python run_watcher.py --verbose
```

Test safely without sending anything:

```bash
.venv/bin/python run_watcher.py --once --dry-run --verbose   # one sweep, detect + log only
.venv/bin/python tools/agent_probe.py "give me a quick farms health summary"
```

The full runbook — background running, launchd service, watching the logs, and
troubleshooting — is in **[HOWTORUN.md](HOWTORUN.md)**.

---

## 6. Configuration

All settings live in `.env` (copy `.env.example`, which documents every key with
its default). The keys you'll touch most:

| Key | Default | Meaning |
|-----|---------|---------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | User-account MTProto creds (from https://my.telegram.org). |
| `TELEGRAM_BOT_TOKEN` | — | The talking bot's token (BotFather). |
| `ALLOWLIST` | — | The owner allow-list — **comma-separated** refs (each a numeric Telegram user id like `1406109190` or an `@username`). The watcher answers any of them (in their own chat) and DMs proactive alerts to **all** of them. Aliases: `ALLOW_LIST`, `ALLOWED_USERS`; legacy `IBO_CHAT_ID` still works as a fallback. First non-empty wins; primary = the first ref. |
| `WATCH_FOLDER` / `WATCH_FOLDER_ID` | `Farms` / `6` | The dialog folder of bots to monitor. |
| `WATCH_POLL_INTERVAL` | `120` | Seconds between proactive sweeps. |
| `MIN_SEVERITY` | `high` | Alert at/above `low`/`medium`/`high`/`critical`. |
| `SILENCE_THRESHOLD_MINUTES` | `120` | Alert if a bot is quiet this long. |
| `PANEL_AUTO_DESTRUCTIVE` | `true` | Run the destructive recovery ladder (Kill all → select 4 → Start) **autonomously** — and the RDP-bug `Reboot PC → Confirm` rung. Set `false` to make destructive steps post a one-tap confirm card instead. |
| `PANEL_LAUNCH_GRACE_MINUTES` | `15` | How long `Accounts launching…` counts as a WAIT state (no relaunch presses, no cold case) before a stuck launch resumes the normal ladder. |
| `RDP_BUG_REBOOT_MINUTES` | `30` | `screen grab failed` persisting this long (panel not operational) triggers the once-per-episode `Reboot PC → Confirm`. |
| `REBOOT_WAIT_MINUTES` | `15` | Quiet verification window after the auto-reboot before resolve-or-cold-case. |
| `PANEL_STALE_MINUTES` | `70` | Total silence (any message resets the clock) before a panel counts as **dead** → cold case `needs PC`. |
| `PANEL_MAX_ATTEMPTS` | `3` | After this many failed recovery cycles in one episode, stop the futile loop and escalate as a cold case (`needs PC`). |
| `DISABLE_AI` | `true` | Fully model-free mode (the default since the AI-removal track): no Ollama, no OpenRouter agent for free-form chat. The monitor loop is model-free regardless of this flag; set `false` only to re-enable the conversational agent. |
| `OLLAMA_URL` / `OLLAMA_MODEL` | local | Legacy local triage model — unused unless `DISABLE_AI=false`. |
| `AGENT_MODEL` | `deepseek/deepseek-v4-pro` | The free-form conversation model (OpenRouter), opt-in via `DISABLE_AI=false`. |
| `NOVEL_RECOVERY` | `true` | Deterministic generic-restart ladder for novel errors (ban/captcha family always exempt). |
| `OVERSEER_SOCKET` / `OVERSEER_TOKEN` | unset | Opt-in overseer endpoint surface (local UNIX socket) — see `docs/wiki/reference/Overseer Endpoints.md`. |
| `OVERSEER_ALLOW_DESTRUCTIVE` | `false` | Lets the external overseer press destructive buttons / run the ladder over the socket. Default off — it can still observe + teach. See the [Hermes Overseer Runbook](docs/wiki/reference/Hermes%20Overseer%20Runbook.md). |
| `AGENT_API_KEY` / `OPENROUTER_API_KEY` | — | Model key (also read from `~/.hermes/.env`). |
| `AGENT_ACTIONS_ENABLED` | `true` | Let the agent DRIVE panels, not just read. |
| `BOT_ACTIONS_ENABLED` | `false` | Let the BOT trigger actions (for `BOT_ACTION_USERS`). |
| `BOT_SELF_EDIT_ENABLED` | `false` | Let an admin make the bot edit its own code. |
| `HOURLY_REPORT_ENABLED` / `HOURLY_REPORT_TOPIC` | `true` / `7` | Hourly report + its forum topic. |

See `.env.example` for the complete, commented list (bot interface, drop-stats →
Sheets, recurring-error watchdog, weekly digest, and the legacy GUI keys).

---

## 7. Architecture (file map)

### Entry points
| File | Job |
|------|-----|
| **`run_watcher.py`** | **The supported entry point.** Loads config + system prompts, then runs `mcp_watcher`. |
| `run_gui.py` | *Legacy.* Screenshot + OCR GUI watcher (no API). See appendix. |
| `run_telegram.py`, `run.py` | *Legacy.* Group watcher / log-file tailer. See appendix. |

### Package `watcherdog/` — the current architecture
| Module | Responsibility |
|--------|----------------|
| `mcp_watcher.py` | The core loop: sweep the Farms folder, route each message, answer ibo, schedule the hourly/weekly/recurring jobs. |
| `bot_interface.py` | The talking **BOT** front-end: commands, Q&A, alert DMs, callback buttons, live progress, task resume, multitasking. |
| `auto_fix.py` | The **script-first router** — classify + learned-fix, then suppress / apply / escalate. Zero LLM for known errors. |
| `learned_fixes.py` | The Markdown **brain** (`data/hermes/learned_fixes.md`): read/match/append known fixes with runnable `action`s. |
| `agent.py` | The read/act tool-calling agent (OpenRouter). Handles novel errors + answers questions; saves fixes. |
| `buttons.py` | Signed single-use inline confirm/action buttons (callback queries handled with no LLM). |
| `roster.py` | Deterministic per-bot health scan (`status` + `reason_code`/`reason_detail`) shared by the hourly report + fast commands. |
| `hourly_report.py` | Pure builder for the layered hourly report — status-grouped sections, incident-joined actions, snapshot diff (`🆕`/recovered/gap). No LLM, no Telethon. |
| `fast_commands.py` / `commands.py` | No-LLM slash commands / AI-prompt slash commands. |
| `tg_tools.py` / `tg_actions.py` | Read-only Telegram helpers / the action layer (drive panels, press buttons). |
| `classifier.py` / `analyzer.py` | Cheap rule prefilter / Ollama triage (`is_error`, severity, summary, fix). |
| `heartbeat.py` | Silence detection (bot went quiet → alert; recovered → notice). |
| `storage.py` | SQLite incident history + de-dup (`data/incidents.db`). |
| `daily_report.py` | The auto-fix log (`daily_errors.jsonl`) + the "fixed this hour / today" summaries. |
| `drop_stats.py` / `drop_sheets.py` | Weekly Wednesday drop-stats job → Google Sheets (skill 5). |
| `task_store.py` | Persists in-progress action tasks so they resume after a restart. |
| `bot_access.py` | Runtime-editable, persisted access grants. |
| `self_restart.py` / `restart_helper.py` | Safe self-restart: pre-flight import check + detached rollback supervisor. |
| `config.py` | Loads `.env` into a typed `Config`. Every tunable lives here. |

### Operating guides — `docs/hermes/`
The agent's system prompt is built from these (they're the durable knowledge, not
hard-coded): `STRUCTURE.md` (the account/folder map), `SKILLS.md`, `TOOLS.md`, and
`skills/00–07` (panels, count farmed, error handling, can't-launch, four-accounts,
drop-stats, stickers, self-improve).

### Tools `tools/`
`tg_login.py` (authorize the user session; recommended recovery command:
`--reset-session --legacy-start`, while plain mode prints the handshake result,
the exact channel Telegram selected for the login code, and any flood-wait;
handles 2FA and `--print-session` to emit a portable StringSession),
`tg_probe.py` (non-interactive MTProto health probe — connects +
checks authorization only; sends NOTHING to your phone, telling a real
handshake/network failure apart from a "just need to log in" state),
`list_dialogs.py` (find chat ids), `agent_probe.py` (ask the agent a question
from the CLI), `simulate_error.py` (log-mode testing), `gui_probe.py` /
`ax_probe.py` (legacy GUI debugging).
`overseer_health.py` (prints a one-line JSON health summary and exits nonzero when the watcher is dead, wedged, or has flagged incidents — the Option-B wake trigger for the Hermes overseer).

---

## 8. Tests

```bash
.venv/bin/python -m pytest          # or: ./scripts/run_tests.sh
```

The suite (`tests/`) covers the router, learned fixes, buttons, fast commands,
fan-out, progress/resume, the deterministic panel-recovery engine, and the
config — **700+ tests** (run `.venv/bin/python -m pytest` for the live count). The
suite is **green** aside from a couple of skipped legacy macOS GUI imports that
need `pyobjc`/`Quartz` (`watcherdog.gui_mac`, `run_gui`).

WatcherDog runs on **Python 3.11–3.14** (entrypoints use `asyncio.run()`; the
local venv is Python 3.14.3 + Telethon 1.43.2, and the MTProto handshake works
fine there).

---

## Appendix: legacy modes

These predate the MTProto watcher and are kept only for reference. They are **not
the supported path** — use `run_watcher.py`.

- **GUI watcher (`run_gui.py`)** — drives the real Telegram macOS app like a
  person: screenshots the window, reads it with Apple Vision OCR, and types
  alerts back. Needs Screen Recording + Accessibility permission, the Mac on /
  unlocked / Telegram visible, and it takes over the mouse and keyboard. Slower
  and more fragile than the API path. Helper probes: `tools/gui_probe.py`,
  `tools/ax_probe.py`. (All `GUI_*` keys in `.env.example` belong to this mode.)
- **Group watcher (`run_telegram.py`)** — an earlier MTProto reader of a single
  group (configured via `WATCH_CHATS`, `ALERT_VIA`). Superseded by the folder-
  based `run_watcher.py`.
- **Log-file watchdog (`run.py`)** — tails `logs/*.log`, detects Python tracebacks
  and `ERROR`/`CRITICAL`/`FATAL`/`Exception` lines, triages with Ollama, alerts
  Telegram. Zero dependencies; only useful if you control a bot's source and want
  it to log into this folder (`watcherdog/bot_logging.py` is a drop-in for that).

> **Auto-recovery of *your machine* is intentionally OFF in every mode.**
> WatcherDog detects, analyzes, alerts, and acts on the **farm panels** (the
> destructive Kill-all → Start ladder runs autonomously by default; see §2) — but
> it never restarts your host or your bots' hosts. A frozen RDP host / black
> screen is only **detected** here (R4 pixel-black, R6 silence) and reported as
> `needs PC`; fixing it (closing/reopening the RDP window, relaunching
> `wfreerdp`) is done by a **separate per-PC tool** in another repo
> ([AdxamAxatov/Watchdog](https://github.com/AdxamAxatov/Watchdog) — `Boot.exe`).
