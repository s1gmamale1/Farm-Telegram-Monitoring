# WatcherDogBot 🐶

A tiny, **zero-dependency** watchdog for your Telegram bots (or any program).
It tails log files, detects errors and Python tracebacks, asks a **local Ollama
model** to triage them (severity + root cause + suggested fix), and sends you a
**Telegram alert** — only for errors at or above a severity you choose.

```
  your bots ──▶ logs/*.log ──▶ WatcherDog monitor ──▶ Ollama (local) ──▶ Telegram ──▶ you
                                      │
                                      └──▶ SQLite incident history (data/incidents.db)
```

## Three modes

All modes share the same brain (classify → Ollama analyze → severity filter →
alert → SQLite history). They differ only in how they read messages and alert:

1. **GUI watcher** (`run_gui.py`) — **no API at all.** Drives the real Telegram
   macOS app like a person: screenshots the window, reads it with on-device OCR
   (Apple Vision), and types/pastes alerts back in. Needs Screen Recording +
   Accessibility permission. Best when you can't/won't use any Telegram API.
2. **Telegram group watcher** (`run_telegram.py`) — logs in as a *user account*
   via MTProto (Telethon) and reads the group, including other bots' messages.
   Needs `api_id`/`api_hash`. More reliable than GUI, but uses the API.
3. **Log-file watchdog** (`run.py`) — tails `logs/*.log`. Zero dependencies.
   Only if you control a bot's source.

> SinFermera are bots, and a Telegram *bot* can't read other bots' messages —
> so the bot API is out. That leaves the GUI watcher (no API) or the MTProto
> user-client. See **"GUI watcher setup"** just below for the no-API route.

---

## GUI watcher setup (no API)

Drives the real Telegram app — reads each bot's latest message from the chat
list, detects problems with Ollama, and pastes an alert into a chat of your
choice. **Reliability caveats:** the Mac must stay **on, unlocked, and with
Telegram visible**; don't use the mouse/keyboard while it runs; it's slower and
more fragile than the API modes.

**One-time setup (you):**
1. **Screen Recording** permission for Terminal: System Settings → Privacy &
   Security → Screen Recording → enable Terminal.
2. **Accessibility** permission for Terminal: same place → Accessibility →
   enable Terminal. (Both are required: capture needs Screen Recording; clicking
   and typing need Accessibility.)
3. In `.env`, set `GUI_ALERT_CHAT` to the **exact sidebar name** of the chat to
   alert (default `Saved Messages` = yourself; or a person's name as it shows in
   your chat list), and confirm `GUI_SEND_KEY` matches your Telegram setting
   (`return` if Telegram sends on Enter, `cmd_return` if on Cmd+Enter).

**Try it (safe — dry run, sends nothing):**
```bash
cd ~/Documents/WatcherDogBot
.venv/bin/python run_gui.py --once     # seeds the visible bots
.venv/bin/python run_gui.py            # runs; logs "[DRY-RUN] would send ..."
```
When you trust what it flags, set `GUI_SEND_ENABLED=true` in `.env` to actually
paste+send alerts.

**Helper probes:** `tools/gui_probe.py` (OCR what's on screen), `tools/ax_probe.py`
(accessibility check).

**Reading all bots:** the watcher **scrolls the chat list** each cycle
(`GUI_SCROLL_STEPS`) so it reads every bot's latest message, not just the
visible ones. Each non-routine message is sent to Ollama, which **decides**
whether it's a real problem.

**Two-way Hermes conversation:** alerts go to the `GUI_ALERT_CHAT` (e.g. `ibo`).
When a **reply** appears in that chat, WatcherDog passes it to your local
**Hermes** agent (`hermes -z … --continue`) and types Hermes's answer back — so
you can chat about the incident and Hermes adapts. A loop-guard stops it ever
replying to its own messages. (Reply detection uses left/right bubble position;
tune `GUI_INCOMING_X_THRESHOLD` if needed. Replies you send from the *same*
account WatcherDog types as won't be seen as "incoming".)

---

## Architecture

| File | Job |
|------|-----|
| `run.py` | Entry point + main loop: poll → analyze → filter → alert → store |
| `watcherdog/config.py` | Loads `.env` |
| `watcherdog/monitor.py` | Tails `logs/*.log`, groups tracebacks, de-dupes, persists read offsets |
| `watcherdog/analyzer.py` | Calls Ollama `/api/chat` for `{severity, summary, root_cause, fix}` |
| `watcherdog/alerter.py` | Sends Telegram messages via the Bot API (stdlib `urllib`) |
| `watcherdog/storage.py` | SQLite incident history |
| `watcherdog/bot_logging.py` | **Drop-in** so your bots write errors where WatcherDog can see them |
| `tools/simulate_error.py` | Writes a fake error to test the whole pipeline |
| `com.watcherdog.monitor.plist` | launchd service (auto-start + auto-restart) |

---

## Quick start

```bash
cd ~/Documents/WatcherDogBot

# 1. Your credentials are already in .env. Confirm a test alert reaches you:
python3 run.py --test
#    -> You should get "✅ WatcherDogBot test alert" in Telegram.

# 2. Start the watchdog:
python3 run.py

# 3. In another terminal, simulate a crash:
python3 tools/simulate_error.py
#    -> Within a few seconds you get an AI-analyzed alert.
```

Stop with `Ctrl+C`.

---

## Telegram group watcher setup (for SinFermera)

This monitors the group where your `SinFermera*` bots post and alerts you when
one reports a problem (ban, Steam Guard, login/proxy failure, crash, etc.).

**One-time setup (you must do steps 1–3 — they need your Telegram identity):**

1. **Get API credentials.** Go to https://my.telegram.org → log in with your
   phone → "API development tools" → create an app. Copy the **`api_id`** and
   **`api_hash`** into `.env` (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`).

2. **Log in once** (use an account that is a member of the SinFermera group):
   ```bash
   cd ~/Documents/WatcherDogBot
   .venv/bin/python tools/tg_login.py
   ```
   Enter your phone number and the code Telegram sends you (+ 2FA password if
   set). A session file is saved to `data/watcher.session`.

3. **Find the chat ids and set them:**
   ```bash
   .venv/bin/python tools/list_dialogs.py
   ```
   Copy the SinFermera chat/group id(s) into `WATCH_CHATS` in `.env`
   (comma-separated, e.g. `WATCH_CHATS=-1001234567890,123456789`).

4. **Choose where alerts go** (`.env`):
   - `ALERT_VIA=user` (default) sends the alert **as your own account** to the
     person in `ALERT_USER` (a `@username`, phone, or numeric id). `ALERT_USER=me`
     sends to your own Saved Messages. *Blank → defaults to `me`, so it never
     messages an unintended contact.*
   - `ALERT_VIA=bot` sends via `@sigmawatchdogbot` to `ALERT_CHAT_ID` instead
     (lowest account risk).

**Run it:**
```bash
.venv/bin/python run_telegram.py            # foreground
```
or always-on:
```bash
cp com.watcherdog.telegram.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.watcherdog.telegram.plist
```

**Test without waiting for a real error:**
```bash
.venv/bin/python run_telegram.py --once-test "[SinFermera2] ERROR: login rejected, account banned"
```

**Notes**
- Automating a *personal* account technically violates Telegram's ToS for
  abusive automation; read-only monitoring is low-risk, but a **dedicated
  secondary account** (added to the group) is the safest choice.
- Routine drop/match/warmup posts are filtered out cheaply; only suspicious or
  unrecognized messages reach Ollama, and the model makes the final call on
  whether something is truly an error before you get pinged.
- Set `ANALYZE_UNKNOWN=false` to only AI-check messages that hit an explicit
  error keyword (fewer Ollama calls, but may miss novel failure wording).

### Silence detection (catching dead/banned bots)
Every message a bot posts counts as a heartbeat. If a bot that normally reports
goes quiet, it probably crashed, got banned, or lost connection — so you get a
`🔕 Bot went SILENT` alert, and a `✅ Back online` notice when it returns.

- Bots are **auto-learned** the first time they post. Set `EXPECTED_BOTS=SinFermera3,SinFermera10`
  to also watch specific bots from startup.
- Tune with `SILENCE_THRESHOLD_MINUTES` (default 30) and `SILENCE_CHECK_INTERVAL_SECONDS`.
- A restart resets each bot's clock (a grace period), so restarting the watcher
  never floods you with false silence alerts. Turn it off with `SILENCE_ENABLED=false`.

---

## Connecting your own bots (log-file mode)

WatcherDog watches the `logs/` directory. Each monitored bot just needs to
write its errors to `logs/<botname>.log`. There are two ways:

### Option A — Python bot (recommended): use the drop-in
Copy `watcherdog/bot_logging.py` into your bot project (or import it) and add
**two lines** at startup:

```python
from watcherdog.bot_logging import install
install("payments")   # -> writes ERROR+ and tracebacks to logs/payments.log
```

Point it at this folder's `logs/` dir from another project with an env var:

```bash
export WATCHERDOG_LOG_DIR=/Users/macmini4/Documents/WatcherDogBot/logs
```

After that, every `logging.error(...)`, `log.exception(...)`, and any **uncaught
exception** (main thread or worker threads) is captured automatically.

### Option B — any language: just write a log file
Have your bot append errors/tracebacks to `logs/<botname>.log`. WatcherDog
detects:
- Python tracebacks (`Traceback (most recent call last):` … exception line)
- any line containing `ERROR`, `CRITICAL`, `FATAL`, or `Exception`

You can also symlink existing logs in:
```bash
ln -s /opt/bots/support/error.log ~/Documents/WatcherDogBot/logs/support.log
```

---

## Configuration (`.env`)

| Key | Default | Meaning |
|-----|---------|---------|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token (already set) |
| `TELEGRAM_CHAT_ID` | — | Where alerts go (already set) |
| `TELEGRAM_THREAD_ID` | _(empty)_ | Forum topic id, if used |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint |
| `OLLAMA_MODEL` | `huihui_ai/gemma-4-abliterated:e4b` | Model used for triage |
| `DISABLE_AI` | `false` | Skip AI, forward raw errors |
| `LOG_DIR` | `logs` | Directory watched for `*.log` |
| `POLL_INTERVAL` | `2.0` | Seconds between checks |
| `MIN_SEVERITY` | `high` | Only alert at/above: low/medium/high/critical |
| `DEDUPE_WINDOW` | `300` | Suppress repeat of same error (seconds) |

Lower `MIN_SEVERITY` to `low` while testing so every error pings you; raise it
back to `high` in production to avoid spam.

---

## Run it 24/7 (launchd)

```bash
cp com.watcherdog.monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.watcherdog.monitor.plist
launchctl list | grep watcherdog        # confirm it's running
```

It auto-starts on login and restarts if it crashes. WatcherDog's own logs go to
`data/service.out.log` / `data/service.err.log`.

Stop it:
```bash
launchctl unload ~/Library/LaunchAgents/com.watcherdog.monitor.plist
```

---

## Notes & limits
- **Auto-recovery is intentionally OFF.** WatcherDog only detects, analyzes, and
  alerts — it never restarts your bots.
- New `*.log` files are read from the **start**; persisted read offsets
  (`data/offsets.json`) mean a restart never replays errors it already saw. If
  you symlink in a large pre-existing log, it will be read once on first sight.
- De-duplication normalizes timestamps / line numbers / addresses so the *same*
  bug doesn't alert repeatedly within `DEDUPE_WINDOW`.
- Sending is direct-to-Telegram on purpose: a watchdog must still reach you when
  other services (including Hermes) are down.
