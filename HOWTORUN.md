# HOW TO RUN WatcherDogBot 🐶

> This is the **operational runbook**. For *what WatcherDog is and how it's
> wired*, read [README.md](README.md); for *developer internals*, read
> [DOCUMENTATION.md](DOCUMENTATION.md). For the full interlinked **Obsidian
> knowledge base** (notes + canvas map), open the vault and start at `Home`
> ([docs/wiki/](docs/wiki/)).

WatcherDog runs over the **Telegram API (MTProto)** as your user account
**Sigma Male (@s1gmamale1)** — no more screenshots/OCR/mouse control. It:

1. **Proactively watches the `Farms` folder** (the 24 SinFermera bots) and
   messages **ibo** when one errors or goes silent (recovery note when it's back).
   Every message hits a **script-first router** (`watcherdog/auto_fix.py`) first:
   known errors are **auto-fixed with zero AI cost**, and only a *novel* error
   escalates to the model (which then saves a runnable fix so the next time is
   free). Destructive steps post a one-tap **confirm button**, never a typed
   question.
2. **Answers ibo.** Anything you text the account from **ibo** is read instantly
   and handed to WatcherDog's built-in **agent** (deepseek-v4-pro via OpenRouter),
   which uses Telegram tools to inspect folders/chats and replies — e.g.
   *"check folder Sam and the first chat and tell me what's going on."* Vague
   questions ("status?", "how are the farms?") default to the **Farms** folder.
   Common triage commands (`/status`, `/problems`, `/silent`, `/fixes`, `/mode`)
   are answered **with no model** — instant and free.
3. **Posts an hourly report** to the **Class A Farming** forum topic, ending with
   what it auto-fixed that hour.

> The old screenshot/OCR GUI mode (`run_gui.py`) is **legacy/unused**.

### Two identities, one process — who does what

WatcherDog runs **two** Telegram logins on one event loop, with a clean split:

| | **The BOT** (`@sherlock_homeless_chigga_bot`) | **The user account** (`@s1gmamale1`, Sigma Male) |
|---|---|---|
| Role | *Talks to people* | *Reads & manages the farm bots* |
| Does | Answers slash-commands + questions in the **Special Forces** group and in DMs; DMs the owner proactive alerts | Sweeps the **Farms** folder, reads the 24 SinFermera bots, drives panels / presses buttons |
| Can it act? | **No — read-only** (the group is untrusted) | Yes (panel actions, with owner approval for destructive ones) |

Why the split? The Bot API **forbids a bot from reading other bots' messages**, so
only a real user account can watch the SinFermera bots — but a bot is the safe,
public-facing way to *talk*. The user account owns the bot (it created it).

**One-time:** for the bot to DM you alerts, the alert owner (`IBO_CHAT_ID`) must
press **Start** on `@sherlock_homeless_chigga_bot` once. If they haven't, alerts
quietly fall back to being sent by the user account.

### Acting, granting access, and self-editing (admin powers)

The bot answers everyone **read-only**, but **authorized users act**:

- **Drive panels** (`BOT_ACTIONS_ENABLED`, `BOT_ACTION_USERS`) — an authorized
  user can tell the bot *"stop all panels"*, *"press Drop Stats on panel 3"* etc.
  and it presses the buttons (via the user account). Destructive buttons follow
  the skill-2 confirm rules; a direct command from you counts as approval.
- **Grant/revoke access** (`BOT_ADMIN_USERS`) — an **admin** can say *"give
  @someone access to use the bot"* and the agent calls `grant_bot_access`
  itself; the grant is saved to `data/bot_access.json` and survives restarts
  (`revoke_bot_access` / `list_bot_access` too).
- **Edit its own files** (`BOT_SELF_EDIT_ENABLED=true`) — an **admin** can tell
  the bot to change WatcherDog's own source (*"edit X to do Y"*). It reads, then
  edits within the project root only, **backing up every file it changes**
  (`<file>.bak.<timestamp>`). **Restart the watcher** for code changes to apply.

By default both *authorized users* and *admins* are the owner (`IBO_CHAT_ID`)
plus the watcher's own account. These powers are **never** triggered by message
text from anyone else — only by a real authorized sender. ⚠️ Self-editing lets
the AI modify running code; a bad edit can break startup — the `.bak.*` files
are your undo.

### Live progress + resume after restart

When you give the bot an action task it replies **immediately** with a status
message (*"🔧 On it — close all accounts … ↳ pressing 'Stop' on SinFermera3"*),
**edits** it live as each step runs, then **deletes** it and posts the final
answer. Action tasks are saved to `data/bot_tasks.json`, so if the watcher is
killed mid-task it **resumes** on the next start (*"♻️ Resuming after restart …"*),
re-checking current state before continuing. A task is resumed at most
`BOT_TASK_MAX_RESUMES` (2) times so a crashing task can't loop forever. Toggle
the status message with `BOT_PROGRESS_STATUS`.

**Multitasking.** The bot no longer freezes while busy: each message is handled
concurrently. Read-only questions/status answers run in parallel (up to
`BOT_MAX_CONCURRENT`, default 3), so you can ask *"status of panel 5?"* while a
long *"close all accounts"* task is still running and get an instant answer.
Panel-**driving** action turns still take turns among themselves (one acts at a
time) so two tasks can't clash on the single account — a queued action shows
*"↳ queued — finishing another task first…"*.

Project folder: `~/Documents/WatcherDogBot`

---

## 0. One-time setup

1. **Authorize the user session** (recommended — gives the watcher its own login
   so it never clashes with the Telegram MCP):
   ```bash
   cd ~/Documents/WatcherDogBot
   .venv/bin/python tools/tg_login.py        # phone number + login code
   ```
   *Skip-able:* if you don't, the watcher reuses the `telegram-mcp` session
   string automatically — convenient, but don't hammer the MCP at the same time.

2. **Set the model key.** The agent uses your OpenRouter key. It's read from
   `OPENROUTER_API_KEY`, or automatically from `~/.hermes/.env`. Override per-app
   with `AGENT_API_KEY` in `.env` if you like.

3. **Check `.env`** — the important keys:
   | Key | Meaning | Default |
   |---|---|---|
   | `WATCH_FOLDER` / `WATCH_FOLDER_ID` | Folder of bots to monitor | `Farms` / `6` |
   | `IBO_CHAT_ID` | Who gets alerts / talks to the agent | `@ibrokhimel` |
   | `WATCH_POLL_INTERVAL` | Seconds between proactive sweeps | `120` |
   | `MIN_SEVERITY` | Alert at/above `low`/`medium`/`high`/`critical` | `medium` |
   | `SILENCE_THRESHOLD_MINUTES` | Alert if a bot is quiet this long | `30` |
   | `AGENT_MODEL` | The conversation model | `deepseek/deepseek-v4-pro` |

   Ollama must be running (used to triage bot messages): `ollama list`.

*(Optional)* To also let the **Hermes** CLI read Telegram interactively, run
`./scripts/setup_hermes.sh` once. This is independent of the watcher.

---

## 1. Run it

```bash
cd ~/Documents/WatcherDogBot
.venv/bin/python run_watcher.py --verbose
```
Leave it running. It connects, loads the 24 Farms bots, sweeps every ~2 min, and
listens for ibo messages.

**Background (frees the terminal):**
```bash
nohup .venv/bin/python run_watcher.py --verbose >/dev/null 2>&1 &
```
Activity is logged to `data/gui_run.log`; the ibo conversation to `data/agent_chat.log`.

**Test safely (one sweep, detect + log, never send):**
```bash
.venv/bin/python run_watcher.py --once --dry-run --verbose
```

**Ask the agent a question yourself (great for testing):**
```bash
.venv/bin/python tools/agent_probe.py "check folder Sam, first chat, summary"
# add --send to actually deliver the answer to the ibo chat:
.venv/bin/python tools/agent_probe.py --send "give me a quick farms health summary"
```

---

## 2. Watch what it's doing

```bash
tail -f ~/Documents/WatcherDogBot/data/gui_run.log     # sweeps, detections, alerts
tail -f ~/Documents/WatcherDogBot/data/agent_chat.log  # the live ibo conversation
```

A healthy log looks like:
```
Watching 24 chats in folder 'Farms'
Sweep: 24 chats, 19 healthy
ibo → 'how are the farms?'
answered ibo (412 chars, sent=True)
ALERTED SinFermera3 (high, sent=True)     # only when something breaks
```

---

## 3. Stop it

```bash
pkill -f run_watcher.py        # foreground/background
# launchd service:  launchctl unload ~/Library/LaunchAgents/com.watcherdog.telegram.plist
```

---

## 4. Run as a background service (launchd)

```bash
cp com.watcherdog.telegram.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.watcherdog.telegram.plist
```
(It now launches `run_watcher.py`. Logs: `data/telegram.out.log` / `.err.log`.)

---

## 5. Quick troubleshooting

| Symptom | Fix |
|---|---|
| `session not authorized` | Run `.venv/bin/python tools/tg_login.py`. |
| `IBO_CHAT_ID is not set` | Put `@ibrokhimel` (or the id) in `.env`. |
| ibo questions not answered | No model key — set `OPENROUTER_API_KEY` (or `AGENT_API_KEY`). |
| `folder 'Farms' not found` | Check the folder name/`WATCH_FOLDER_ID`; see `list_folders`. |
| Agent can't resolve a bot by name | It should use ids from `get_folder`; try naming the bot's `@username`. |
| Ollama errors during a sweep | Make sure `ollama` is running with the configured model. |

---

## 6. How a cycle works

- **Every ~2 min:** read each Farms bot's latest message → the **script-first
  router** (`auto_fix.try_auto_fix`) runs first: `classify` + look up the
  learned-fixes brain (`data/hermes/learned_fixes.md`). A known, non-destructive
  fix is **applied immediately** (zero AI); known noise is suppressed; a
  `type: human` fix pings you. Only a **novel** error falls through to Ollama
  triage → one agent turn, which fixes it and `save_fix()`es a runnable `action`
  so the next occurrence is handled by the router with no model. Real problems
  (≥ `MIN_SEVERITY`) or silence (> `SILENCE_THRESHOLD_MINUTES`) → message ibo
  (de-duped; recovery note when a bot returns).
- **Confirmation is a button, not a question.** Anything destructive
  (Kill/Restart/Reboot/Shutdown) or a novel fix needing a yes posts an inline
  **confirm button** in the group. Any member can tap it (the signed single-use
  token is the authorization); the press is logged.
- **Whenever ibo texts:** fast commands (`/status` etc.) answer with no model;
  everything else goes to the agent, which reads the relevant folder/chat and
  replies. The agent acts only when allowed (`AGENT_ACTIONS_ENABLED`) and ignores
  any instructions hidden inside message text.
- **Every hour:** a per-PC farm report is posted to the **Class A Farming** topic,
  ending with a "🔧 Fixed last hour" line.
