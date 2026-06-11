# Phase 6 — Vision-on-demand via the overseer

**Date:** 2026-06-12
**Status:** Approved (standing approval; decisions recorded below)
**Track:** AI-removal (ROADMAP ADR-001) · FINAL phase · follows Phase 5 (PR #16)

## Problem

Two signals are screenshot-only: the `farmed/N` total (image), and the
"panel flagged, recovery failed, message text explains nothing" diagnosis.
The deterministic core rightly can't read images. Phase 5 gave the overseer
eyes on the incident queue (`list_flagged`) and hands (`press_button` etc.) —
but no way to *see* a panel, and the queue never receives the panel cold-cases
that are precisely the "only vision can diagnose this" moments.

## Decisions

1. **Vision data is PULLED fresh, not pushed stale.** No screenshot files
   carried on incidents, no new schema. The overseer calls a new
   `screenshot(bot)` endpoint when it picks up an incident — a capture taken at
   diagnosis time, not at flag time.
2. **Panel cold-cases enter the overseer queue.** `_open_panel_incident` opens
   with `novel=True` (reusing the Phase 4 flag and the Phase 5 `list_flagged`
   queue — zero schema change). `fixable=False` unchanged: the followup
   nag/give-up baseline is untouched, so with NO overseer connected, behavior
   is exactly today's.
3. **`farm_stats.needs_vision` (farmed/N) stays a rendering `?`** — a standing
   condition, not an incident. The overseer sees it via `get_stats` and may
   screenshot at will; opening incidents for every image-only number would
   spam the queue.
4. **The vision read itself lives in the overseer** (outside this repo). The
   core never gains an image dependency — ADR-001 holds.

## Design

### A. 9th endpoint: `screenshot`

In `watcherdog/overseer_api.py`:

| method | params | wraps | returns |
|---|---|---|---|
| `screenshot` | `bot` | `tg_actions.screenshot(client, ent, cfg=cfg)` | `{"downloaded": <path>, "caption": ...}` or `{"error": ...}` |

- Roster-only `_entity` resolution like every bot endpoint.
- Dry-run gated like `press_button` (it presses the panel's Screenshot button —
  non-destructive, but still a press): under `deliver=False` → error response,
  no press.
- The returned path is host-local; the UNIX socket guarantees the overseer is
  on the same host, so a path is sufficient (no media streaming — YAGNI).

### B. Cold-cases flagged for the overseer

`mcp_watcher._open_panel_incident` (`:351`): `tracker.open("panel", name,
f"panel:{name}", "high", summary, fixable=False, novel=True, now=now)` — the
single-line change that routes "needs PC / can't launch" episodes into
`list_flagged`. The summary already names the cold-case type; the overseer
screenshots for the visual diagnosis.

### C. The vision loop (documented in `docs/wiki/reference/Overseer Endpoints.md`)

`list_flagged` → a `source: "panel"` row (or a stuck `bot_error`) →
`screenshot(bot)` → the overseer's vision model reads the image →
`press_button` / `resolve_flagged` / `teach_fix`. Disconnected overseer →
the row stays a nagged human alert (deterministic baseline preserved).

## Error handling

`_h_screenshot` mirrors the other handlers: unknown bot / dry-run / press
failure → error response, never crashes the watcher. `tg_actions.screenshot`'s
own `{"error": ...}` shapes pass through as the result (they're diagnostic).

## Testing

- `screenshot` endpoint: happy path (fake `tg_actions.screenshot` returns
  `{"downloaded": "/tmp/x.jpg"}`), dry-run refusal (no press, mutation-proof
  per the press_button pattern), unknown-bot error.
- `_open_panel_incident` wiring: a real tracker → row has `novel == 1` and
  `source == "panel"`; `novel_list()` (the queue) contains it.
- Docs updated. Green-check via `git ls-files`; mutation-verify the dry-run
  gate and the `novel=True` flag.

## Files

| file | change |
|---|---|
| `watcherdog/overseer_api.py` | `_h_screenshot` + registry entry |
| `watcherdog/mcp_watcher.py` | `_open_panel_incident` opens `novel=True` |
| `docs/wiki/reference/Overseer Endpoints.md` | endpoint row + vision-loop section |
| `tests/test_overseer_api.py` | screenshot endpoint tests |
| `tests/test_novel_recovery.py` (or tracker tests) | cold-case novel-flag test |

## Execution

Inline, worktree → TDD → mutation-verify → reviewer pass → PR → merge →
roadmap marker. This completes the AI-removal track; the Hermes overseer agent
itself (the client) is a separate project.
