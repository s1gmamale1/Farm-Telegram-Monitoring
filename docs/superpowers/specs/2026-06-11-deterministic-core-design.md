# Deterministic Core (AI-removal track) — Design (2026-06-11)

## Problem

The runtime path still calls models: **Ollama** for per-message triage severity and
**OpenRouter** (via `agent.answer`) for novel errors, the report commands, and free-form Q&A.
The owner's standing direction (memory: `reduce-ai-firm-preference`) is to make the **core
script 100% model-independent** and move all AI to an optional Hermes overseer that drives the
script through endpoints (ROADMAP ADR-001). This spec covers the **deterministic core** — the
foundation for that removal — scoped to what is buildable now plus the shared contracts the
later phases depend on.

## What the exploration changed

- **`watcherdog/farm_stats.py` already exists** — a fail-safe `parse_panel_status(text) ->
  PanelStatus` (launched/status/map/score/total/accounts/updated_at; "never guess a number").
  Phase 1 is *extend to a richer `BotStats`*, not build-from-scratch.
- **All 9 report command handlers already exist** in `watcherdog/commands.py` (`_weekly`,
  `_today`, `_top`, `_worst`, `_value`, `_check`, `_bans`, `_compare`, `_whatsnew`) — but they
  currently EXPAND into prompts for `agent.answer` (OpenRouter). Phase 2 is *make them compute
  deterministically from `BotStats`*, not write new commands.
- **The production log has zero structured status cards** — every `[watcherdog.mcp]` line shows
  `status=None`; the parser's current test fixtures (`📊 Panel status: ├ Launched: 4…`) appear
  spec-derived, not confirmed live traffic; hermes skill-01 says farmed/total is often
  screenshot-only. **Therefore Phases 1–2 cannot be trustworthily designed without real
  samples.** (Owner decision below: capture mode first.)
- **`watcherdog/classifier.py` already yields `error/normal/unknown` deterministically** with
  mature regex families, so Phase 3 (triage) needs no samples and is buildable now.

## Decisions (owner-confirmed)

- **D1 — Fixtures: capture mode first.** Before the parser, build a one-run capture utility; the
  owner runs it against the live fleet; the parser + fixtures are built from those real samples.
  No parser is built on guessed formats.
- **D2 — Novel errors: full ADR-001, zero runtime model calls.** When deterministic triage hits
  a novel error it can't classify (no regex match, no learned fix), the runtime posts a plain
  alert + a one-tap recovery action card and records a `needs_human`/`needs_vision` flagged
  incident the overseer can read later. No Ollama, no OpenRouter on the hot path.

## Sequencing & parallelism (the reframe)

```
Phase 0  Capture tool ──► (owner runs it) ──► real samples
              │                                  │
Stream B  Phase 3 triage  ◄── buildable NOW       ▼
          (drop Ollama)        (disjoint file)     Stream A: Phase 1 parser ─► Phase 2 reports
              │                                                  │
              └─────────── converge ──► Phase 4 novel-error ──► Phase 5/6 overseer
```

- **This increment (buildable now, no samples):** the **capture tool** + **Phase 3 triage**.
  Disjoint code (`scripts/`+capture vs `mcp_watcher.py:_evaluate_bot`); neither needs samples;
  parallel-safe in separate worktrees.
- **Sample-blocked:** Phases 1–2 wait until the owner runs capture. They get their own short
  brainstorm once real samples land — designing their parse detail now would be guessing.
- **Later:** Phase 4 after Phase 3; Phases 5–6 after the core is deterministic.

## Shared contracts (locked now so Phases 3–4 can depend on them)

### `BotStats` (extends `PanelStatus`)
```
{bot, drops, value_usd, accounts_up, accounts_total, items[], banned[], captcha[],
 last_status, problems[], data_source: "text"|"missing", needs_vision: bool}
```
**Hard invariant:** every field is `Optional`; anything not present as parseable TEXT stays
`None` and sets `data_source`/`needs_vision` — the parser MUST never raise and never fabricate a
number. (The exact field set and parse regexes are finalized against captured samples; the
*shape* and the fail-safe rule are locked here.)

### `severity_of(text)` / `summarize(text)` (deterministic, no model)
Derived from the SAME `classifier.py` regex families already in production:
ban/captcha/Steam-Guard → critical/high; generic error indicator → high; unknown → **high
conservative fallback** (matches the current `_fallback`, so a mis-rank surfaces, never hides).
`summarize` returns a short deterministic line from the matched signal + a bounded excerpt.

### Flagged incidents — REUSE `IncidentTracker`, not a parallel store
A novel/unparseable case opens an incident on the existing `open_incidents` ledger with a
`needs_human` / `needs_vision` marker (a `source` value or a column flag — finalized in the
plan). The overseer (Phase 5) reads open flagged rows via the same tracker API. No second store.

## Components

- **`scripts/capture_panel_formats.py`** (Phase 0) — read-only: for each panel in the roster,
  `/start`, press the stats buttons, dump raw reply text + button labels + any screenshot
  reference to `data/captures/<panel>.txt`. No fixing, no model. Owner-runnable; output becomes
  `tests/fixtures/`.
- **`watcherdog/farm_stats.py`** (Phase 1, after samples) — extend `parse_panel_status` → a
  `parse_bot_stats(texts) -> BotStats` per the locked contract; fail-safe.
- **`watcherdog/roster.py`** (Phase 1) — extend the no-LLM `scan` to populate `BotStats`
  alongside the existing status buckets.
- **`watcherdog/commands.py`** (Phase 2) — the 9 handlers compute from aggregated `BotStats`;
  routing change so they resolve before/instead of `commands.expand → agent.answer`;
  `needs_vision` numbers render `?` not a guess.
- **`watcherdog/mcp_watcher.py:_evaluate_bot`** (Phase 3) — reorder so `classify()` +
  `learned_fixes.find_fix()` run first; severity/summary from `severity_of`/`summarize`; novel
  unmatched error → deterministic alert + action card + flagged incident; config default flips
  to the deterministic path (model becomes legacy-only).

## Data flow (Phase 3, the now-buildable removal)

panel message → `classify()` → (normal → resolve/skip) / (error → `learned_fixes.find_fix()`:
match → run scripted fix; miss → `severity_of`/`summarize` → alert + one-tap card +
`flagged_incident`) — **no model call anywhere on this path.**

## Error handling

Fixture-driven and fail-safe end to end: the parser returns `None` fields + `needs_vision`,
never raises, never fabricates; `severity_of` defaults novel → `high`; the capture tool degrades
a non-responding panel to a noted gap rather than blocking. Phase 3 keeps the existing
recurring-error and incident-lifecycle backstops (already on main from the campaign).

## Testing

TDD with the campaign's tiered-review discipline (per-fix mutation-verification; full review on
subtle/object-touching tasks; holistic pass before merge). Phase 3 tests assert: classify-first
ordering, deterministic severity for the known families, the novel-error path makes **zero**
model calls (assert no `analyze_message`/`agent.answer`/urlopen), and a flagged incident is
created. Parser tests (Phase 1) are built from captured fixtures: every captured sample parses
to the expected `BotStats`, and unparseable input yields `None`/`needs_vision`, never a fabricated
value or an exception.

## Out of scope (this increment)

Phases 1–2 parse detail (own brainstorm after samples), Phase 4 action-ladder detail (after
Phase 3), Phases 5–6 (Hermes overseer endpoint surface + vision-on-demand), free-form Q&A
(stays on the overseer), and the 8 deferred Phase-D infra items in `WISHLIST.md`.
