# HOW TO RUN WatcherDogBot 🐶

A simple operator's guide — how to start, stop, watch, and fix it. (For what
each file does see `DOCUMENTATION.md`; for ideas/TODOs see `WISHLIST.md`.)

Project folder: `~/Documents/WatcherDogBot`

---

## 0. Before you start (every time)

The script controls the real Telegram app, so:

- ✅ **Telegram is open and its window is visible** (not minimized or fully covered).
- ✅ **The Mac is unlocked and won't sleep.** To stop sleep, run in a spare Terminal:
  `caffeinate -d &`
- ✅ **Ollama is running** (`ollama list` should show `huihui_ai/gemma-4-abliterated:e4b`).
- ✅ **Terminal has Screen Recording + Accessibility permission**
  (System Settings → Privacy & Security). Already granted — only redo this if you
  reinstall/upgrade Terminal or macOS.
- 🖱️ **Don't touch the mouse/keyboard during a sweep** (it clicks through all 24
  chats for ~1–2 min each cycle).

---

## 1. Start it

```bash
cd ~/Documents/WatcherDogBot
.venv/bin/python run_gui.py --verbose
```

Leave that Terminal window **open** (the permission is tied to it). On the first
cycle it reads all 24 bots and sends **"everything working perfectly"** to the
**ibo** chat.

### Start it in the background instead (keeps running, frees the Terminal)
```bash
cd ~/Documents/WatcherDogBot
nohup .venv/bin/python run_gui.py --verbose > data/gui_run.log 2>&1 &
```
(Still keep that Terminal window open.)

### Just test once (one sweep, then exits)
```bash
.venv/bin/python run_gui.py --once
```

---

## 2. Watch what it's doing

```bash
# main activity log (reads, detections, alerts):
tail -f ~/Documents/WatcherDogBot/data/gui_run.log

# the live Hermes conversation with ibo:
tail -f ~/Documents/WatcherDogBot/data/hermes_chat.log
```
A Terminal window tailing the Hermes chat also opens automatically.

What a healthy cycle looks like in the log:
```
Read 24 bot chats by opening each.
All good — status to 'ibo' (sent=True)        # first run / after a recovery
...next cycles stay quiet unless a bot errors...
ALERTED ... / Pasted+sent alert to 'ibo'       # when something breaks
```

---

## 3. Stop it

```bash
pkill -f run_gui.py
```
Verify it's gone: `pgrep -lf run_gui.py` (no output = stopped).

---

## 4. What it does each cycle (every ~2 min)

1. Opens each of the 24 bot chats, reads the latest message.
2. Sends anything non-routine to Ollama, which decides if it's a real problem.
3. **Problem** → messages **ibo** with what's wrong + a suggested fix.
   **All fine** → sends "everything working perfectly" **once** (not repeated).
4. If **ibo replies**, hands it to **Hermes**, which answers back in the chat.

---

## 5. Settings you might change (`.env`)

Edit `~/Documents/WatcherDogBot/.env`, then restart (stop + start).

| Setting | What it does | Default |
|---|---|---|
| `GUI_ALERT_CHAT` | Which chat gets alerts (exact sidebar name) | `ibo` |
| `GUI_SEND_ENABLED` | `true` = really send; `false` = dry-run (log only) | `true` |
| `GUI_POLL_INTERVAL` | Seconds between sweeps | `120` |
| `MIN_SEVERITY` | Alert at/above: `low`/`medium`/`high`/`critical` | `medium` |
| `GUI_MAX_BOTS` | How many bot chats to read | `24` |
| `GUI_STATUS_MESSAGE` | The "all good" text | ✅ everything working perfectly |
| `GUI_SEND_KEY` | `return` if Telegram sends on Enter, else `cmd_return` | `return` |
| `HERMES_ENABLED` | Let Hermes auto-reply to messages in ibo | `true` |

**Safe testing tip:** set `GUI_SEND_ENABLED=false` to watch it detect without
messaging anyone, or point `GUI_ALERT_CHAT=Saved Messages` to test on yourself.

---

## 6. Quick troubleshooting

| Symptom | Fix |
|---|---|
| "Telegram window not found" | Open Telegram, make its window visible, unlock the Mac. |
| Clicks the wrong place | Don't move/resize the Telegram window while it runs; restart. |
| It types but doesn't send | Set `GUI_SEND_KEY` to match your Telegram "send by" setting. |
| Doesn't find `ibo` | Make sure a chat named exactly `ibo` exists; it scrolls to find it. |
| Misses some bots | Raise `GUI_SCROLL_MAX`; pin the SinFermera chats near the top. |
| Ollama slow first time | Normal — the model loads on the first call (~20–30s). |
| Hermes silent | Test manually: `~/.local/bin/hermes -z "test" --continue watcherdog` |
| Reads weird text | Run `.venv/bin/python tools/gui_probe.py` to see what OCR sees. |

---

## 7. After a reboot

1. Open Telegram, log in if needed, keep the window visible.
2. Make sure Ollama is running.
3. `cd ~/Documents/WatcherDogBot && .venv/bin/python run_gui.py --verbose`

That's it. Stop with `pkill -f run_gui.py` whenever you want it off.
