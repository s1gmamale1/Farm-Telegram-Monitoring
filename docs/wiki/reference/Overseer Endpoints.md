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

The socket file is created `0600` (umask-guarded during bind, so there is no
permissive window). Note: a pre-existing parent dir (e.g. `data/`) keeps its
own permissions — protection rests on the socket file itself, so **setting
`OVERSEER_TOKEN` is recommended** even though the surface is local-only.
Request lines are capped at 64 KB. Every call is audit-logged, including
unauthorized and unknown-method requests; confirmed destructive presses are
additionally recorded to the daily fix log with the actually-pressed label.
Under a dry run, `press_button` refuses and `run_ladder` returns `skipped` —
the overseer can never press what the loop couldn't.

Teaching policy: `teach_fix` rejects control characters in every field (the
brain file is line-oriented), and refuses `auto: yes` combined with a
destructive `action` — the overseer may teach a destructive fix, but the first
recurrence goes through the existing confirm card so the owner keeps confirm
authority. Teach destructive fixes with `auto: ""`.

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
| `screenshot` | `bot` | `tg_actions.screenshot` (presses the panel's Screenshot button, downloads the image) | `{"downloaded": <host-local path>, "caption": ...}`; refused under dry-run |

`bot` resolves against the **watch roster only** (exact name, case-insensitive,
or bot number — `"SF7"`/`"7"`/`"SinFermera7"`); anything else is an error. A
raw Telegram username is never resolved.

## The overseer loop

`list_flagged` → `read_bot` (investigate) → `press_button`/`run_ladder` (fix)
→ `teach_fix` (so next time is script-only) → `resolve_flagged`.

## The vision loop (Phase 6)

Panel cold-cases ("needs PC" / can't-launch episodes — the points where the
core has exhausted text-based understanding) are flagged `novel=1`, so they
appear in `list_flagged` alongside novel bot errors. The overseer then calls
`screenshot(bot)` for a FRESH capture (stale images are useless — vision data
is pulled at diagnosis time, not carried on the incident), reads the image
with its own vision model (outside this repo — the core never gains an image
dependency), and acts via `press_button` / `resolve_flagged` / `teach_fix`.
The image-only `farmed/N` total stays a `?` in reports (`get_stats` exposes
`needs_vision`); it is a standing condition, not an incident. With no overseer
connected, cold-cases remain nagged human alerts — the deterministic baseline
is unchanged.

## Reference client

```
OVERSEER_SOCKET=data/overseer.sock python -m scripts.overseer_cli list_flagged
python -m scripts.overseer_cli --socket data/overseer.sock \
    press_button '{"bot": "SinFermera7", "button": "drop stats"}'
```

Note: macOS caps UNIX-socket paths at ~104 bytes — keep `OVERSEER_SOCKET`
short (e.g. `data/overseer.sock`). A too-long path fails to bind; the watcher
logs it and continues without the surface.
