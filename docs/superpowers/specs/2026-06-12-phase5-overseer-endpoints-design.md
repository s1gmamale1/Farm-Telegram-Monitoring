# Phase 5 — Hermes overseer endpoint surface

**Date:** 2026-06-12
**Status:** Approved (owner granted standing approval; design presented and accepted)
**Track:** AI-removal (ROADMAP ADR-001) · follows Phase 4 (PR #15)

## Problem

The deterministic core is complete (Phases 1–4): the monitor loop never calls a
model, and novel errors are flagged `novel=1` on IncidentTracker. But nothing
can *consume* that queue or teach fixes back without re-coupling AI into the
core. The overseer needs a defined seam: observe the fleet, investigate a
flagged incident, drive a panel, teach the resulting fix to `learned_fixes`,
and close the incident — all from OUTSIDE the process, with the core importing
zero AI.

## Decisions (locked)

1. **Endpoints (8):** `list_flagged, read_bot, list_buttons, press_button,
   run_ladder, get_stats, resolve_flagged, teach_fix`. `grant_access` (in the
   roadmap's draft list) is dropped — YAGNI, and an overseer granting itself
   powers is a capability smell. `teach_fix` is added — the learn-loop
   (novel → fix → teach → script-only next time) is the point of the overseer.
2. **Opt-in:** the server only starts when `OVERSEER_SOCKET` (a filesystem
   path) is configured. Unset = no socket, zero new surface on existing
   deploys.
3. **Transport (per the approved roadmap):** newline-delimited JSON over a
   local UNIX socket. No network exposure, stdlib only.
4. **Out of scope:** migrating `agent.py` onto the endpoints (wishlist),
   Phase 6 vision, any TCP/HTTP transport.

## Design

### A. `watcherdog/overseer_api.py` (new, ~250 ln, stdlib only)

`async def serve(client, cfg, state, deliver=True)` — `asyncio.start_unix_server`
on `cfg.overseer_socket`. The socket's parent dir is created `0o700` and the
socket chmod'd `0o600` after bind. One JSON object per line:

```
request:  {"id": 1, "method": "list_flagged", "params": {...}, "token": "..."}
response: {"id": 1, "result": ...}   |   {"id": 1, "error": "message"}
```

Handlers call the SAME functions the loop uses (no new capability paths):

| method | params | wraps | returns |
|---|---|---|---|
| `list_flagged` | — | `tracker.novel_list()` (tracker from `state["tracker"]`) | list of incident rows |
| `read_bot` | `bot`, `limit=15` | watch-roster entity lookup (`_entity_for`-style via `state["watch"]`) + `tg_tools.read_history` | `[{"text", "date"}, ...]` |
| `list_buttons` | `bot` | `tg_actions.panel_menu` | labels list |
| `press_button` | `bot`, `button`, `confirmed=False` | `tg_actions.press_button` | press result dict |
| `run_ladder` | `bot` | `novel_recovery.attempt` (all gates honored) | attempt outcome |
| `get_stats` | — | `fleet_report.snapshot(client, cfg, watch)` | JSON-serialized fleet (dataclasses → dicts) |
| `resolve_flagged` | `id`, `resolution` | NEW `tracker.resolve_by_id(id, resolution)` (~10 ln, mirrors `escalate_by_id`: only mutates an OPEN row, returns bool) | `{"resolved": bool}` |
| `teach_fix` | `signature`, `match`, `fix`, `action=""`, `auto=""`, `type="ai"` | `learned_fixes.append_fix(path=cfg.learned_fixes_path, added_by="overseer", date=<today>)` | the written fix dict (findability is asserted in tests, not in the handler) |

Entity resolution: `read_bot/list_buttons/press_button/run_ladder` resolve the
bot name against `state["watch"]` (same roster the loop drives); unknown bot →
error response, never a raw Telegram username resolution (the stranger-DM
lesson from the refix loop).

### B. Auth + safety

- If `cfg.overseer_token` is set, every request must carry `"token"`; compare
  with `hmac.compare_digest`. Missing/wrong → `{"error": "unauthorized"}`.
- `press_button` refuses destructive labels unless explicit `confirmed: true`
  (the `tg_actions.is_destructive` gate, same as everywhere); a confirmed
  destructive press is recorded to `daily_report` (`added_by` overseer).
- `deliver` (dry-run) propagates from the watcher: under dry-run,
  `press_button` returns `{"error": "dry-run"}` without pressing and
  `run_ladder` hits `attempt`'s internal gate (`skipped`).
- Every call → one audit log line: `OVERSEER <method> bot=<...> ok=<...>`.
- Robustness: per-connection and per-request try/except → error responses;
  request line capped at 64 KB; malformed JSON / unknown method / missing
  params → error response. The server NEVER crashes the watcher; `serve` is
  wrapped so even a bind failure only logs.

### C. Wiring + config

- `config.py`: `self.overseer_socket = get("OVERSEER_SOCKET", "").strip()`
  (resolved via `resolve_path` when non-empty) and
  `self.overseer_token = get("OVERSEER_TOKEN", "").strip()`.
- `mcp_watcher` main wiring (next to `_incident_followup_loop` startup): if
  `cfg.overseer_socket`, `client.loop.create_task(overseer_api.serve(client,
  cfg, state, deliver))`. Stale socket file from a crash is unlinked before
  bind.

### D. Reference client `scripts/overseer_cli.py` (new, ~60 ln stdlib)

`python -m scripts.overseer_cli <method> [json-params]` — connects to
`$OVERSEER_SOCKET` (or `--socket`), sends one request (token from
`$OVERSEER_TOKEN`), pretty-prints the response. Satisfies the roadmap DoD and
is Hermes's integration reference.

### E. Docs

`docs/wiki/reference/Overseer Endpoints.md` — the endpoint table, protocol
example, auth, and the CLI invocation (a roadmap deliverable).

## Error handling

Never-raises at the boundary: every handler returns an error response instead
of raising; the serve loop survives client disconnects mid-request; JSON
serialization of rows/dataclasses goes through a `default=str` fallback so an
odd value can't kill a response.

## Testing (`tests/test_overseer_api.py`, real objects)

Real UNIX socket in tmp + real `IncidentTracker`/fixes file:
- auth: token required when configured (reject without/wrong, accept with);
  no token configured → open.
- `list_flagged` round-trips a real `novel=1` row.
- `teach_fix` writes a block that `learned_fixes.find_fix` then finds; file
  parseable (`load_fixes`).
- `press_button` destructive label without `confirmed` → refused (no press —
  fake tg_actions records calls); with `confirmed` → pressed.
- `run_ladder` under dry-run → `skipped`, no press.
- `resolve_flagged` closes the open row; second call → `{"resolved": false}`.
- malformed JSON and unknown method → error response, server still answers the
  next request.
- Mutation-verify: auth compare, destructive gate, resolve_by_id open-only
  guard.

## Files

| file | change |
|---|---|
| `watcherdog/overseer_api.py` | **new** — serve + 8 handlers |
| `watcherdog/incident_tracker.py` | `resolve_by_id` |
| `watcherdog/config.py` | `OVERSEER_SOCKET`, `OVERSEER_TOKEN` |
| `watcherdog/mcp_watcher.py` | opt-in serve task |
| `scripts/overseer_cli.py` | **new** — reference client |
| `docs/wiki/reference/Overseer Endpoints.md` | **new** — endpoint reference |
| `tests/test_overseer_api.py` | **new** |

## Execution

Inline (this session): worktree → TDD task-by-task → mutation-verification →
reviewer pass → PR → merge-if-clean → roadmap marker.
