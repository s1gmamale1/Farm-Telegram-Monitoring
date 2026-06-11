# Overseer Endpoints

The Phase 5 seam: an external Hermes overseer (the only place AI lives)
observes and drives the deterministic core through a **local UNIX socket** —
the core imports no model. Opt-in: the watcher binds the socket only when
`OVERSEER_SOCKET` is set in `.env`. Optional shared secret via
`OVERSEER_TOKEN` (each request must then carry `"token"`).

## Protocol

One JSON object per line (ndjson), request → response:

```
→ {"id": 1, "method": "list_flagged", "params": {}, "token": "..."}
← {"id": 1, "result": [...]}            (or {"id": 1, "error": "message"})
```

Socket file is `0600` in a `0700` dir; request lines are capped at 64 KB.
Every call is audit-logged. Under a dry run (`--dry-run`), `press_button`
refuses and `run_ladder` returns `skipped` — the overseer can never press
what the loop couldn't.

## Endpoints

| method | params | wraps | returns |
|---|---|---|---|
| `list_flagged` | — | `IncidentTracker.novel_list()` | open `novel=1` incident rows (the overseer queue) |
| `read_bot` | `bot`, `limit=15` | `tg_tools.read_history` (watch-roster entity only) | `{chat, id, messages:[{from,date,text}]}` |
| `list_buttons` | `bot` | `tg_actions.panel_menu` | panel inline-button labels |
| `press_button` | `bot`, `button`, `confirmed=false` | `tg_actions.press_button` | press result; destructive labels refused without `confirmed:true` (confirmed destructive presses are recorded to the daily fix log) |
| `run_ladder` | `bot`, `text=""` | `novel_recovery.attempt` (all gates honored: needs_human, NOVEL_RECOVERY, dry-run) | attempt outcome (`attempted/failed/skipped/human_needed`) |
| `get_stats` | — | `fleet_report.snapshot` | the fleet (per-bot status, drops, value) |
| `resolve_flagged` | `id`, `resolution` | `IncidentTracker.resolve_by_id` | `{"resolved": true|false}` (false = already closed/unknown) |
| `teach_fix` | `signature`, `match`, `fix`, `action=""`, `auto=""`, `type="ai"` | `learned_fixes.append_fix` (added_by `overseer`, dated) | the written fix block |

`bot` resolves against the **watch roster only** (exact name, case-insensitive,
or bot number — `"SF7"`/`"7"`/`"SinFermera7"`); anything else is an error. A
raw Telegram username is never resolved.

## The overseer loop

`list_flagged` → `read_bot` (investigate) → `press_button`/`run_ladder` (fix)
→ `teach_fix` (so next time is script-only) → `resolve_flagged`.

## Reference client

```
OVERSEER_SOCKET=data/overseer.sock python -m scripts.overseer_cli list_flagged
python -m scripts.overseer_cli --socket data/overseer.sock \
    press_button '{"bot": "SinFermera7", "button": "drop stats"}'
```

Note: macOS caps UNIX-socket paths at ~104 bytes — keep `OVERSEER_SOCKET`
short (e.g. `data/overseer.sock`). A too-long path fails to bind; the watcher
logs it and continues without the surface.
