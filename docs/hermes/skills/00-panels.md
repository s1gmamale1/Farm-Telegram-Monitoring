# Skill 0 — Panels & the `/start` menu (reference)

## What a panel is
One **panel** = one PC running the CS2/Steam farm, controlled through its own
Telegram **control bot**. Panels live in the **Panels** folder (`PANELS_FOLDER`,
see `.env`). Each chat in that folder is one panel; the number in its display
name is the **Panel#** (e.g. "Panel 3" → `Panel#3`). Ask ibo to confirm the
roster once; cache it, but re-check with `get_folder` since membership changes.

## How to drive a panel
1. `send_message(panel, "/start")` — the bot replies with its inline-button menu.
2. `list_inline_buttons(<that message>)` — read the buttons (text can be cut off).
3. `press_inline_button(message, button)` — press the one you need.
4. Wait, then `get_messages`/`download_media` to read the result.

## The `/start` menu (buttons → meaning)
| Button (as shown) | What it does |
|---|---|
| **Screenshot** | Panel sends a screenshot of the PC. Your eyes on the machine. |
| **Launch...s stats** (Launchers stats) | Status of launchers / how many accounts up. |
| **Sel...farmed** (Select farmed) | Select the already-farmed accounts. |
| **Sel...10 accs** (Select 10 accs) | Select a batch of 10 accounts. |
| **Select accounts manually** | Pick specific accounts by hand. |
| **Make lobbies and search game** | Create lobbies and queue a match. |
| **Start selected accounts** | Launch the currently-selected accounts. |
| **Kill All CS & Steam** | Force-close all CS2 + Steam. ⚠ destructive. |
| **Look selected accounts** | Show which accounts are currently selected. |
| **Drops Stats** | Request the drop statistics (skill 5). |
| **Run activity booster** | Start the activity booster. |
| **Steam Route Tool [ON]** | Toggle the Steam route tool (shows ON/OFF). |
| **Set Timer For Autofarm** | Set the autofarm timer. |
| **Collect purple accounts** | Collect the "purple" accounts. |
| **Panel settings menu** | Open the panel's settings submenu. |
| **Restart panel** | Restart the panel software. ⚠ confirm with ibo. |
| **Reboot PC** | Reboot the whole PC. ⚠ confirm with ibo. |
| **S..own PC** (Shutdown PC) | Shut the PC down. ⚠ confirm with ibo. |

> Button labels are truncated in Telegram. Match by **prefix**, and re-read with
> `list_inline_buttons` rather than trusting this list blindly.

## Recovery toolkit (safe order, least → most disruptive)
For most "stuck/won't launch" problems, escalate in this order, checking a
**Screenshot** between steps:
`Screenshot` → `Start selected accounts` → `Kill All CS & Steam` → re-select
(`Sel...10 accs` / `Sel...farmed`) → `Start selected accounts` → `Restart panel`
→ (only with ibo's OK) `Reboot PC`.
