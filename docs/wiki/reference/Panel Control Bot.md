---
title: Panel Control Bot
tags:
  - watcherdog
  - reference
  - telegram
updated: 2026-06-08
status: current
---

# Panel Control Bot

> The SinFermera FSM-panel control bot — its auto-updating status message (what WatcherDog *reads*) and its full inline-button menu (what WatcherDog *presses*). This is the authoritative action vocabulary for [[Monitoring and Recovery Rules]].

Part of [[Home]]. Each `SinFermeraN` chat in the Farms folder is one **FSM panel** (one farming PC) controlled through this bot — see [[Two Identities One Process]] for why the user account, not a bot, drives it.

> [!info] One panel = one PC running 4 CS2 accounts
> The bot belongs to the third-party **FSM Panel** app on each PC. WatcherDog reads its messages and presses its buttons over MTProto ([[Telegram Tools and Actions]]); it supersedes the on-PC `Watchdog.exe` babysitter described in [[Legacy Modes]].

## The status message (what we parse)

The bot keeps one auto-updating "FSM Panel - Main menu" message. It is the primary detection source for [[Monitoring and Recovery Rules]] and the data source for [[Roster and Health Scan]] / [[Drop-Stats Pipeline]]:

```
📟 FSM Panel - Main menu 📟
User: SinFermera7
HWID: 914139A1...

📊 Panel status:
├ 👥 Launched: 4 accounts
├ 🟢 Status: LIVE
├ 🗺 Map: de_nuke
└ 🏆 Score: [1:0]

🎮 Accounts:
├54. lilpro51
│  📊 LVL: 14 | XP: 3686 | 🟩
├52. nuggetgoat_irl8574
│  📊 LVL: 14 | XP: 1519 | 🟩
├56. womplord2
│  📊 LVL: 13 | XP: 1680 | 🟩
├49. meowrizz2
│  📊 LVL: 15 | XP: 1542 | 🟩
└ ✅ Total: 4
⏱ Updated: 23:03:50
```

| Field | Parse target | Meaning |
|-------|--------------|---------|
| `Launched: N accounts` | int `N` | **Must be ≤ 4.** The core health signal. |
| `Status: <LIVE/…>` | string | `🟢 LIVE` = operational. |
| `Map` + `Score` | strings | Present + progressing ⇒ actually in a match (farming). Absent/stale ⇒ may be idle. |
| `Accounts:` list + `Total: N` | per-account `id, name, LVL, XP, 🟩` | The launched roster. |
| `Updated: HH:MM:SS` | time | Freshness of the status. |

Free-text alerts also appear, e.g. `[SinFermera24] All 8 accounts launched!` — an explicit over-launch signal.

## The button menu (what we press)

Buttons as shown in the bot's `/start` menu, top to bottom. ✅ = used by recovery, ⚠ = destructive (must confirm via [[Confirm and Action Buttons]]).

| Button | What it does | Recovery role |
|--------|--------------|---------------|
| 🖼 **Screenshot** | Sends a screenshot of the PC | ✅ confirm state; **black image ⇒ RDP-session host bug ⇒ per-PC API restart** (cold case) |
| 📊 **Launched accs stats** | Status of launched accounts | optional active read |
| 👉 **Select 4/10 unfarmed** | Selects 4 **un-farmed** accounts (does **not** launch) | ✅ **the canonical selection** |
| 👉 **Select first 4/10 accs** | Selects the first 4 regardless of farmed state | ❌ **do not use** |
| 👉 **Select accounts manually** | Opens the full account list; tap to select | avoid — more overhead than Kill-all flow |
| 👥 **Make lobbies and search game** | Starts the farm/match if it isn't running | ✅ fixes idle "sits there" panels |
| 🟢 **Start selected accounts** | Launches the currently-selected accounts | ✅ |
| 🔴 **Kill all CS & Steam** | Force-closes all CS2 + Steam | ✅ ⚠ the reset lever for over-launch |
| 📋 **Drop Stats** | Requests weekly drop stats | ✅ weekly job (run with 0 launched) → [[Drop-Stats Pipeline]] |
| ⚡ **Run activity booster** | Starts the activity booster | ✅ run **after** Drop Stats |
| 🔘 **Steam Route Tool [ON]** | Toggles the Steam route tool | not needed |
| ⏰ **Set Timer for Autofarm** | Sets the autofarm timer | not needed |
| 🟣 **Collect purple accounts** | Collects "purple" accounts | not needed |
| ⚙️ **Panel settings menu** | Opens a settings submenu | not needed |
| 🔄 **Restart panel** | Restarts the panel software | ⚠ confirm |
| 🔄 **Reboot PC** | Reboots the whole PC | ⚠ confirm (last resort) |
| ⛔ **Shutdown PC** | Shuts the PC down | ⚠ confirm |

> [!warning] Match buttons by label prefix, re-read live
> Labels can change/truncate. The action layer ([[Telegram Tools and Actions]]) resolves a button by exact → prefix → substring match from a live read of the inline keyboard (`tg_actions.press_button`), not a hard-coded index — same discipline as the `docs/hermes/skills/00-panels.md` skill guide. The named wrappers and their `BTN_*` label constants live in `panel_actions.py` ([[Module Reference]]).

## See also
- [[Monitoring and Recovery Rules]] — the deterministic rules that consume this vocabulary
- [[Telegram Tools and Actions]] — the layer that presses these buttons
- [[Drop-Stats Pipeline]] — the weekly Drop Stats + activity-booster sequence
- [[Confirm and Action Buttons]] — how destructive presses are gated
- [[Roster and Health Scan]] — parses the status message for health
- [[Home]] — knowledge-base index
