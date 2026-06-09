# Deep-Review Fix Campaign — Design (2026-06-10)

## Problem

A 5-agent audit (panel FSM, incident lifecycle, bot-eval/channels, supporting modules, 24h log
forensics) found 22 confirmed defects. Production evidence from `data/gui_run.log`:
38 PC-off HIGH alerts in 24h, 10 incidents for ONE dead PC at an exact ~71-minute period,
dual-path doubles on SinFermera11/16/24, 4 incidents leaked 30+ hours, the followup "refix"
never having worked, the hourly report dead all day, and dry-run sending real messages.
Full cited index: `WISHLIST.md` → "Deep review findings (2026-06-10)".

## Root-cause model (what the fixes must respect)

Three structural truths explain almost everything:

1. **The watcher contaminates its own evidence.** `latest_message` counts the watcher's own
   outgoing `/start` probe as panel activity (no `m.out` filter), and the FSM clears its
   episode latch on any non-flag decision. Every probe therefore resets both the staleness
   clock and the one-alert-per-episode memory → the 71-minute alert storm, broken ✅ closure,
   eternal re-probing.
2. **Resolution authority is too coarse while episode state is too fragile.** Traffic-based
   signals (any fresh message; a stale "normal" line) close health-based incidents across all
   sources, while the real episode identity lives in process-memory latches that die on every
   restart even though the ledger rows persist. False ✅, false "❌ needs PC", orphans, churn.
3. **Per-sweep re-evaluation of the same latest message** floods the store, fabricates 🔁
   counts, keeps dedupe hashes eternally fresh (suppressing real alerts), and wastes model calls.

## Approach

Four phases, ordered by owner-visible value; each phase is one branch → PR → two-stage review
(spec compliance, then code quality + /code-review) → push → merge, with regression tests per
fix written test-first (the tracked suite stays the authority: `pytest $(git ls-files 'tests/*.py')`).

- **Phase A — Kill the alert storm** (FSM evidence + latch integrity). Fixes the probe
  contamination at the source, makes the two PC-off paths share one episode, makes restarts
  quiet, repairs latch lifecycle (r2/coldcase resets), and drives closure off the durable
  tracker row. Outcome: a dead PC = exactly ONE HIGH per episode + scheduled followups +
  one give-up; recovery always produces exactly one closure signal; restarts re-alert nothing.
- **Phase B — Make the lifecycle truthful** (resolution semantics + suppression scope).
  Freshness-gated resolves, source-scoped silence resolution, startup re-arm from the ledger,
  hash/severity-aware suppression, re-open inside the dedupe window, same-message memo,
  row-id-keyed followup writes, refix that actually reaches the panel entity. Outcome: every
  ✅/⏳/❌ message is true; new errors are never hidden; refix presses real buttons.
- **Phase C — Robustness + dry-run + hourly** (blast-radius control). Sweep exception
  isolation, both dry-run leaks sealed, hourly report fallback-or-loud-disable. Outcome: one
  bad chat costs only itself; dry-run touches nothing real; hourly report delivers or says why not.
- **Phase D — Infra hardening** (lower-frequency failure modes). Drop-stats zero-panel
  failure handling, restart-supervisor guards, alert truncation, per-bot dedupe scope,
  executor offloading, lock unification, SF-listener hardening, copy/duration fixes.

## Key design decisions

- **D1 — Outgoing-message filter lives in `latest_message`** (fetch `limit=3`, take first
  `not m.out`), not at call sites: every consumer of "panel activity" gets the fix.
- **D2 — Latch clearing requires `decision.healthy`** (a parseable fresh healthy card), not
  merely "not flagging". `coldcase_reported` clears on any fresh *parseable* card (healthy or
  not) because the PC is demonstrably back — actionability is what matters.
- **D3 — The durable `open_incidents` row is the episode identity.** `had_episode`/resolution
  consults `tracker.open_for_bot("panel", name)` in addition to in-memory latches; at startup,
  open panel rows re-arm `coldcase_reported` so restarts neither re-alert nor falsely escalate.
- **D4 — Resolves prove health, not traffic.** The classify-"normal" resolve requires the
  message be newer than `opened_ts`; the silence-recovery branch resolves only
  `resolve_by_bot("silence", …)` (the scoped resolver that exists but was never wired).
- **D5 — Suppression compares substance.** The bot_error gate alerts + refreshes the open row
  when the new hash differs or severity rises; otherwise suppresses as today.
- **D6 — One evaluation per message.** `state` memoizes the last evaluated message id/hash per
  bot; unchanged latest message ⇒ skip analysis, recording, and dedupe refresh entirely.
- **D7 — Dry-run = zero external writes:** no sends, no button presses, no ledger mutations.
- **D8 — Silence channel stays panel-shadowed for now** (documented), because the FSM probe
  covers dead-PC and Phase A makes it truthful. The "alive-but-unproductive" gap is a separate
  wishlist enhancement, not snuck into this campaign.

## Testing strategy

Each fix lands with a regression test that replays the production failure: probe-then-sweep
(own `/start` as latest), R6-then-selfreport double, restart-with-open-rows, stale-normal
resolve, error-arrives-while-silent, new-critical-while-suppressed, refix-entity resolution,
dry-run ledger purity, hourly-report empty target. The FSM tests gain the one fixture family
the audit proved missing: message streams that include the watcher's own outgoing traffic.

## Out of scope

`kill_all` failures on the PC side (Watchdog repo), the alive-but-unproductive silence gap
(wishlist), AI-on markup stripping (wishlist), Phase 1–6 AI-removal roadmap track (unchanged).
