# Phase 2 — Deterministic report commands

**Date:** 2026-06-11
**Status:** Approved (design) — pending spec review
**Track:** AI-removal (ROADMAP ADR-001) · follows Phase 1 (BotStats parser, PR #13)

## Problem

Under `DISABLE_AI=true` (the default since `dbafaa7`), the fleet's report and
triage commands dead-end. The 9 model-backed `commands.py` handlers
(`/weekly /today /top /worst /value /check /bans /compare /whatsnew`) expand into
prompts for `agent.answer` (OpenRouter); when AI is disabled they all fall through
to `no_ai_reply` — a generic "AI disabled, send 'drop stats'" string. So the
common money/triage questions return nothing useful on a default deploy. These
commands are the last non-deterministic hole in the runtime path.

Separately, the existing weekly drop-collection records **wrong** numbers. Its
parser `drop_stats.parse_drop_stats` was written against a guessed format. On a
real panel Drop Stats reply:

```
➙ Price of all drop: ~ 31.5$.
➙ Total cases: 28 pcs.
Accounts: 28
```

it returns `{'drops': '31', 'items': '', 'value': '28'}` — it grabbed `31` from the
**price** and `28` from the **account count**. The truth is *28 cases worth $31.50*.
The buffer and the Google Sheets push have been recording garbage. There is no
correct historical data to preserve.

## Goal

Make the report commands compute from real fleet data with **zero model calls** —
dropping OpenRouter from the report path entirely (deterministic even when AI is
enabled), and fix the collection parser so the numbers are correct.

Out of scope: per-day drop tracking, cross-folder unread scanning, vision for the
image-only `farmed/N` figure (Phase 6), self-edit (`/improve`).

## Decisions (locked in brainstorm)

1. **Data source — buffer-backed + parser convergence.** Money commands read the
   existing weekly drop buffer (instant, free, no farm interruption). The weekly
   collection switches to the Phase 1 parser so the buffer finally holds correct
   `$`/cases/skins. Numbers are as fresh as the last Wednesday/on-demand collection
   — reports label that staleness.
2. **Scope — 6 deterministic + 1 reframed, 2 unchanged.**
   `/weekly /value /top /worst /check /compare /bans` → fully deterministic.
   `/today` → reframed as "current live status + this week's drops so far" (a weekly
   buffer cannot show true per-day drops). `/whatsnew` → stays `no_ai_reply`
   (cross-folder unread, not a farm-stats query). `/improve` → unchanged (admin
   self-edit).
3. **Drop the model from the report path entirely** — the report commands are
   deterministic regardless of `DISABLE_AI`. The model conversation path remains for
   free-form questions, `/whatsnew`, and `/improve` only.

## Design

### A. Parser convergence (`drop_stats.py`)

`collect_week` parses each panel's Drop Stats reply with `farm_stats.parse_drop_report`
(Phase 1) instead of `parse_drop_stats`. A small pure adapter maps the structured
`DropReport` onto the sheet columns (`drop_sheets.COLUMNS`):

| sheet column | source |
|---|---|
| `drops` | `DropReport.total_cases` |
| `value` | `DropReport.value_usd` |
| `items` | `len(DropReport.skins)` (valuable skins ≥ $0.6 pulled) |
| `notes` | `"no reply"` / `"dry-run"` preserved; else `""` |

- The destructive `stop_farm → request_drop_stats → run_activity_booster` sequence is
  **untouched** — only the parse step changes.
- `make_row` continues to receive a dict keyed for `COLUMNS`; the adapter produces
  that dict. `format_report` is unchanged (it already shows `· ~$value` when present —
  now `value` is actually populated and correct).
- `parse_drop_stats` is **removed** (it produces actively-wrong output; keeping it as
  dead code is misleading). Its 3 tracked tests in `tests/test_drop_stats.py` are
  replaced with tests for the new adapter. (The `parse_drop_stats` references in the
  untracked `tests/test_coverage_gaps*.py` are stale and excluded from the green-check
  via `git ls-files` — they are not updated.)

### B. New module `fleet_report.py` (pure formatters + one cheap sweep)

```
snapshot(client, cfg, watch) -> list[BotStats]
```

One `latest_message` sweep per bot — cheap, mirrors `roster.scan`, **no button
presses**. For each bot it reuses the roster helpers (`load_pc_map`,
`classify_status`, `extract_account_count`) for live status/pc/age, runs
`farm_stats.parse_status_event` on the latest text, and **merges** the matching row
from the latest weekly buffer (`drops`, `value_usd`, `cases`, `skins`) keyed by bot
number. Returns a `list[BotStats]`. Never raises — a single bot's failed read or a
missing buffer leaves its fields `None`/empty.

Buffer resolution: load the current ISO-week buffer (`drop_stats.iso_week(now)` →
`buffer_path`); if absent, fall back to the most recent `<YYYY-Www>.json` in
`DROP_STATS_DIR`. The collection date and week id are carried into the formatters for
the staleness label.

Live status (pc/age/status bucket) is not a field on `BotStats`; `snapshot` returns
each `BotStats` paired with its roster info (a light wrapper or a parallel dict keyed
by bot) so the triage formatters can show "farming/quiet/dead" + age alongside drops.

Pure formatters (each takes the snapshot + week/collected label, returns a phone-sized
string):

| command | formatter | content |
|---|---|---|
| `/weekly` | `weekly` | headline total cases + ~$total; per-bot drops·$; top 3 / bottom 3 |
| `/value` | `value` | grand total $; top contributing bots |
| `/top` | `top(n=5)` | best bots by value (then drops) |
| `/worst` | `worst(n=5)` | laggards: lowest value / stalled / silent |
| `/check <n>` | `check(bot)` | one bot: live status + age + drops/$ + top cases |
| `/compare <a> <b>` | `compare(a, b)` | two bots side by side |
| `/bans` | `bans` | bots whose latest msg = ban / captcha / Steam-Guard (classifier strong family) |
| `/today` | `today` | live roster status snapshot + "this week's drops so far (collected …)" |

Rendering rules:
- A bot with no buffer row shows `?` for drops/$ (never a fabricated 0).
- `needs_vision` (the image-only `farmed/N`) is never shown as a number — omitted or
  rendered `?`.
- Empty fleet buffer → reports say "no drop collection yet — send `drop stats` to pull
  now" (points at the existing destructive collection).
- Every money report carries a one-line staleness footer: week id + collection date.

### C. Dispatch wiring

- `fleet_report.handle(cmd, args, *, cfg, client, watch) -> str` — analogous to
  `fast_commands.handle`. Takes the snapshot once, dispatches to the formatter,
  returns the reply text. Never raises (returns a friendly error line on failure).
- A `REPORT_NAMES` set in `commands.py` lists the 8 deterministic report commands
  (`weekly today top worst value check compare bans`).
- Both dispatchers route those names to `fleet_report.handle` **before** the model
  branch:
  - `mcp_watcher.py` `_on_ibo` (~`:1116`–`:1131`): after the `fast_parse` block, add a
    `commands.report_parse`/`REPORT_NAMES` check → `fleet_report.handle`, return.
  - `bot_interface.py` (~`:600`–`:619`): the same insertion after its fast block.
- `/whatsnew` and free-form requests keep their current behaviour (model when AI on,
  `no_ai_reply` when off). `/improve` unchanged.
- `run_weekly_digest` (`mcp_watcher.py:1367`) renders the deterministic
  `fleet_report.weekly` instead of `commands.expand("/weekly") + agent.answer`, and
  **no longer early-returns under `DISABLE_AI`** — the digest becomes free and
  always-on. (It still requires the client/roster; it does not stop farms.)

## Error handling

- `snapshot` never raises: per-bot read failures and a missing/corrupt buffer degrade
  to `None`/empty fields (consistent with `roster.scan` and `farm_stats` "never
  raises" contract).
- `fleet_report.handle` wraps the snapshot+format in a try/except → a one-line
  "couldn't build that report" reply, mirroring `fast_commands` error handling.
- The adapter and formatters are pure and total over `None`/empty inputs.

## Testing

- **Parser convergence:** `collect_week`/adapter produce a correct row from real Drop
  Stats text (`drops=28, value=31.5, items=3`) — a regression proving the old garbage
  (`drops=31, value=28`) is gone. Dry-run and "no reply" rows still marked.
- **`snapshot` merge:** roster live status + a real buffer file → expected
  `list[BotStats]`; missing buffer → drops/$ `None`; single bad bot read doesn't abort.
- **Formatters:** golden-ish assertions over a synthetic `list[BotStats]`, including
  empty-buffer (`?`), `needs_vision` (no fabricated number), top/bottom ordering, and
  the staleness footer.
- **Dispatch:** a report command routes to `fleet_report.handle` with **no
  `agent.answer`** call, under `DISABLE_AI` both `true` and `false`; `/whatsnew` still
  hits the model/`no_ai_reply`.
- Green-check via `pytest $(git ls-files 'tests/*.py')`; mutation-verify each fix
  (revert → test fails).

## Files

| file | change |
|---|---|
| `watcherdog/fleet_report.py` | **new** — `snapshot` + 8 formatters + `handle` (<500 ln) |
| `watcherdog/drop_stats.py` | `collect_week` uses `parse_drop_report` via new adapter; remove `parse_drop_stats` |
| `watcherdog/commands.py` | add `REPORT_NAMES` (+ a `report_parse` helper if cleaner) |
| `watcherdog/mcp_watcher.py` | route report cmds in `_on_ibo`; deterministic `run_weekly_digest` |
| `watcherdog/bot_interface.py` | route report cmds before the model branch |
| `tests/test_drop_stats.py` | replace 3 `parse_drop_stats` tests with adapter tests |
| `tests/test_fleet_report.py` | **new** — snapshot, formatters, dispatch |

## Execution

Sequential-ish (adapter → snapshot → formatters → wiring → digest), run via
subagent-driven-development with tiered review (full review on the snapshot/merge and
the dispatch wiring; mutation-verification on the mechanical formatter/adapter tasks),
then a holistic cross-commit review before the PR. Worktree → PR → reviewer pass →
merge-if-clean.
