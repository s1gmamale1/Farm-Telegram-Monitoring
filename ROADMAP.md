# WatcherDog — Roadmap

WatcherDog is a single-process Telegram monitor for ~24 `SinFermera*` CS2/Steam drop-farming panels, run from a real **user account** (MTProto/Telethon) so it can read the bots and press their inline buttons. Today it already drives panels and handles known errors deterministically, but it still calls a **local Ollama** model for triage and an **OpenRouter** model for novel errors, the report commands, and free-form Q&A. The committed direction is to make the **core script 100% AI-independent** and move all AI to an **optional Hermes overseer** that drives the script through well-defined endpoints. The detailed source of truth for the current architecture is the Obsidian vault at [`docs/wiki/`](docs/wiki/) (start at `Home`); the panel/format spec is [`docs/hermes/`](docs/hermes/).

This ROADMAP is the single source of truth for what to build next.

---

## How to read this

- **Phases are ordered by value/effort**, with cross-phase prerequisites called out.
- **Effort** is S (≤½ day), M (1–2 days), L (3–5 days), XL (>1 week).
- Confirmed bugs (if any) are fixed before new feature phases.
- The guiding rule for every phase: **the runtime path must not import or call a model.** When the script can't resolve something deterministically (needs vision or judgment), it does the deterministic thing — **flags it for a human / the overseer** — never an inline model call. See **ADR-001**.

---

## Confirmed bugs to fix first (hotlist)

| # | Sev | Bug | Where (file:line) | Effort |
|---|-----|-----|-------------------|--------|
| 1 | ✅ | ~~Two concurrency tests fail on a pristine checkout~~ — **RESOLVED 2026-06-08** (fixed via test fixtures; full suite green, those `test_bot_interface` tests pass) | `tests/test_bot_interface.py` | M |
| 2 | Low | Unpinned runtime deps not declared: `gspread`/`google-auth` (drop-stats → Sheets, optional) and `pyobjc` (legacy GUI). `requirements.txt` pins only `telethon` | `requirements.txt` | S |
| 3 | Low | `agent.py` module docstring says "READ-ONLY loop" but it exposes act/self-edit/grant/restart tools (it's READ/ACT) | `watcherdog/agent.py:2` | S |
| 4 | Low | Stray space inside f-string braces renders a double space in the "needs attention" hourly line | `watcherdog/mcp_watcher.py:724` (`f'…{bn }…'`) | S |

---

## Phase 1 — Deterministic farm-stats parser  ⭐ do first

**Goal.** A bot's recent Telegram **text** is parsed into a structured per-bot record with zero model calls; anything not in text is explicitly marked unknown (never guessed).

**Deliverables.**
- `watcherdog/farm_stats.py` — `parse_messages(texts) -> BotStats` returning `{bot, drops, value_usd, accounts_up, accounts_total, items[], banned[], captcha[], last_status, problems[], data_source: "text"|"missing"}`.
- A `needs_vision: bool` flag set when a known signal (farmed/total, Drops Stats) is referenced but only present as an image/attachment.
- `tests/test_farm_stats.py` — fixtures of real/representative panel messages → asserted parses.
- Per-panel fixtures derived from the operator-provided **panel-tools repo** (the software on each PC that emits the messages) — the authoritative format source.

**Why now.** Every report command and the deterministic triage depend on extracting `drops / value / farmed / bans` from text. This parser is the linchpin; nothing else in the AI-removal effort works without it.

**Scope.**
- Reuse the existing regexes in `watcherdog/classifier.py:19-40` (price tails `- 0.27$`, ban/captcha/Steam-Guard, drop/warmup/match lines) as the seed vocabulary.
- Add parsers for the formats named in `docs/hermes/skills/01-count-farmed.md` (`Farmed/Total`, launcher stats) and `05-drop-stats.md` (`N drops · ~$xx`, weekly totals).
- Extend `watcherdog/roster.py` (the current no-LLM scan) to populate `BotStats` instead of just status buckets.
- Every field is `Optional`; a field that can't be parsed stays `None` and sets `data_source`/`needs_vision` — the parser must never raise and never invent a number.

**Findings + recommendation.** `classifier.py` already proves the text is regex-tractable for detection; the hermes skills show the bots post `Farmed/Total` and `drops · $value` **as text first, screenshot only as fallback**. Recommendation: build a pure-function parser against fixtures, treat screenshot-only data as `needs_vision` (Phase 6 territory), and validate against real captures before flipping any command over.

**Risks.** *No live message samples exist yet* (fresh checkout, no `data/`), so formats are inferred from the spec. Mitigation: parser is fixture-driven and fail-safe (unknown → `None`, not a wrong number); add a one-run capture mode to collect real samples, then harden. **This phase needs real sample messages from the operator to be trustworthy.**

**Definition of done.** `parse_messages()` returns correct fields for the fixture set; unparseable input yields `None` fields + `needs_vision`/`missing`, never an exception or a fabricated value; `pytest tests/test_farm_stats.py` green.

---

## Phase 2 — Deterministic report commands

**Goal.** `/weekly /today /top /worst /value /check /bans /compare /whatsnew` and the weekly digest answer from parsed text with **no model call**.

**Deliverables.**
- Deterministic handlers (mirroring `watcherdog/fast_commands.py`) for all nine commands + the scheduled weekly digest.
- Routing change so these resolve before/instead of `commands.expand → agent.answer`.
- `tests/test_report_commands.py`.

**Why now.** This is the largest **token-cost** removal (these are the only commands that spend OpenRouter dollars on a routine basis) and it's unblocked the moment Phase 1 lands.

**Scope.**
- Add handlers that aggregate `farm_stats.BotStats` across the watch folder; format with the exact shapes in `docs/hermes/skills/01` and `05`.
- Rewire `watcherdog/commands.py:255` (`expand`) consumers in the ibo/bot path so these commands hit the deterministic handler; keep `static_reply`/`fast_commands` as-is.
- For any number that is `needs_vision`, render `?` with a short "(image only — ask the overseer)" note instead of calling a model.
- Repoint `run_weekly_digest` (`watcherdog/mcp_watcher.py`) at the deterministic `/weekly` handler.

**Findings + recommendation.** The command prompts in `commands.py:35-146` already enumerate exactly which fields each report needs (drops, value, top/bottom, bans) — they are a precise spec for the deterministic versions. Recommend a 1:1 port, command by command, each behind its own test.

**Risks.** Loss of nuance vs. a model's free-form summary. Mitigation: the hermes output shapes are already terse/structured, so determinism matches intent; `needs_vision` items degrade to `?` rather than silently dropping.

**Definition of done.** Each command returns a correct report from fixture data with **zero** model calls (assert no `agent.answer`/urlopen in the path); image-only metrics show `?`.

---

## Phase 3 — Deterministic triage (drop Ollama from the runtime path)

**Goal.** Error detection, severity, and dedupe run with no Ollama call; the local model is off the hot path by default.

**Deliverables.**
- Deterministic severity/summary derivation (rules: ban/captcha/Steam-Guard → critical/high; generic error → high; etc.) replacing `analyzer.analyze_message` on the monitor path.
- Reordered `_evaluate_bot` so `classify()` + `learned_fixes.find_fix()` run **first** (fixing the documented ordering drift).
- Config default flipped so the deterministic path is the default; Ollama becomes strictly opt-in.

**Why now.** Removes the per-message model call (latency + the last "AI in detection" dependency) and corrects the script-first ordering the docs already claim.

**Scope.**
- New `severity_of(text)` / `summarize(text)` helpers (deterministic) used by `watcherdog/mcp_watcher.py:_evaluate_bot` (~`:261-339`).
- Move `auto_fix.try_auto_fix` ahead of any triage; only fall through to detection for truly unknown text.
- `analyzer.py` retained for legacy modes only; not imported on the `run_watcher` path.

**Findings + recommendation.** Deep dive confirmed Ollama currently runs *before* the router (`_evaluate_bot`), contradicting the "script-first" claim; and `classifier.py` already yields `error/normal/unknown` deterministically. Recommend deriving severity from the same regex families and reserving Ollama for legacy `run.py` only.

**Risks.** Deterministic severity may mis-rank a novel phrasing. Mitigation: conservative default (unknown error → `high`, matching the current `_fallback`), tunable rules, and the recurring-error watchdog still catches repeats.

**Definition of done.** With Ollama unconfigured, a sweep detects/severities/dedupes correctly; no Ollama HTTP call on the `run_watcher` path; existing `test_analyzer`/`test_monitor` still green for legacy use.

---

## Phase 4 — Deterministic novel-error handling (drop OpenRouter from the runtime path)

**Goal.** A router miss never calls a model; it produces a deterministic human alert plus a one-tap recovery action card, and can still be *taught* a runnable fix.

**Deliverables.**
- A "novel error → alert + action card" path replacing `_incident_via_agent` on the core loop.
- A configurable **safe recovery ladder** runner (`Screenshot → Start selected → Kill All → re-select → Start → Restart panel`, per `docs/hermes/skills/00`/`03`) offered as confirm buttons.
- A `flagged_incidents` store (needs-human / needs-vision) the overseer can read (feeds Phase 5).

**Why now.** This is the last OpenRouter dependency in the runtime path; after it, the core never imports `agent.py`.

**Scope.**
- In `watcherdog/mcp_watcher.py:_evaluate_bot`, replace the `_incident_via_agent` branch with `buttons.relaunch_options`-style cards + a `flagged_incidents.add(...)`.
- Keep `learned_fixes.append_fix` reachable from an ibo reply / button so fixes are still taught (no model needed to *record* a fix).
- Gate `agent.py` entirely behind the overseer (Phase 5); core imports drop it.

**Findings + recommendation.** Skill 2/3 already define the deterministic escalation ladder and the "ask once, then save a runnable action" loop — the model was only ever choosing among a fixed button vocabulary. Recommend running the fixed ladder behind confirm buttons and flagging anything that needs a screenshot read.

**Risks.** Some genuinely novel errors won't auto-resolve. Mitigation: that's the intended design — flag for the human/overseer rather than guess; the learned-fixes brain shrinks the tail over time.

**Definition of done.** A simulated novel error triggers **no** model call, posts an alert + confirm card, and creates a `flagged_incident`; teaching a fix via reply makes the next occurrence router-only.

---

## Phase 5 — Hermes overseer endpoint surface

**Goal.** An external Hermes agent can monitor the script and perform manual fixes through a defined interface, without the core importing any AI.

**Deliverables.**
- A local command surface (CLI/JSON-RPC over a socket, or an in-proc bus exposed via a thin HTTP server) covering: `read_bot`, `list_buttons`, `press_button`, `run_ladder`, `get_stats`, `list_flagged`, `resolve_flagged`, `grant_access`.
- An auth/allowlist gate on the surface (reuse `bot_access.py` semantics).
- `docs/wiki/` note + endpoint reference; `tests/test_endpoints.py`.

**Why now.** Once the core is deterministic (Phases 1–4), the overseer is the home for all AI — this phase is the seam that lets Hermes drive without re-coupling.

**Scope.**
- Wrap the existing read layer (`tg_tools.py`) and action layer (`tg_actions.py`) behind the endpoint surface; the surface calls the **same** scripted functions the loop uses.
- Publish `flagged_incidents` (Phase 4) over `list_flagged`; `resolve_flagged` runs a chosen action and records to the brain.
- `agent.py` becomes the overseer client that calls these endpoints — it lives outside the runtime path.

**Findings + recommendation.** `tg_actions`/`tg_tools` are already clean function layers (the agent only ever *called* them), so exposing them as endpoints is mechanical. Recommend a minimal local JSON-RPC over a UNIX socket (no network exposure) with an allowlist.

**Risks.** A new surface is new attack surface. Mitigation: local-only socket, `bot_access` allowlist, every destructive action still goes through the confirm-button/`auto` gate.

**Definition of done.** `curl`/CLI can `list_flagged` and drive a panel end-to-end through the endpoint with the core importing no model; auth rejects non-allowlisted callers.

---

## Phase 6 — Vision-on-demand via the overseer (optional)

**Goal.** Screenshot-only signals (farmed/total, "can't launch" diagnosis) are resolved by the overseer's vision, with the core still AI-free.

**Deliverables.**
- A `needs_vision` flagged-incident type carrying the screenshot reference.
- An overseer handler that reads the image and calls `resolve_flagged`.

**Why now.** Closes the one capability gap the deterministic core can't cover; only worth doing after Phases 1–5.

**Scope.** Emit `needs_vision` from `farm_stats` (Phase 1) and the novel-error path (Phase 4); overseer downloads the media, reads it (vision model), and posts the resolution via Phase 5 endpoints.

**Findings + recommendation.** Skills 1/3 make clear that reading a PC screenshot is the irreducible AI task. Recommend keeping it 100% in the overseer so the core never gains an image dependency.

**Risks.** Overseer availability. Mitigation: if no overseer is connected, `needs_vision` items simply stay flagged for a human — the deterministic baseline still holds.

**Definition of done.** A screenshot-only "can't launch" produces a `needs_vision` flag; with the overseer connected it's resolved via endpoint; with it disconnected it remains a human alert. Core unchanged.

---

## Architecture decisions (ADRs)

### ADR-001 — Deterministic core, AI as an external overseer
**Decision.** The runtime path (`run_watcher.py` → `mcp_watcher`) must never import or call a model. All AI (Ollama, OpenRouter, vision) lives in an optional **Hermes overseer** that drives the script through the Phase 5 endpoints.
**Context.** AI was embedded for triage, reports, novel-error fixing, and Q&A; the operator wants the script autonomous and AI-independent, with AI additive rather than load-bearing.
**Consequences.** (+) The farm keeps running, reporting, and applying known fixes with zero model dependency, latency, or token cost; AI is hot-swappable. (−) Free-form Q&A and screenshot/judgment cases are unavailable unless the overseer is connected (they degrade to deterministic human alerts).

### ADR-002 — Screenshot/vision boundary → flag, never inline-model
**Decision.** When a needed signal exists only as an image or requires visual judgment, the core emits a `needs_vision` flag and a human alert; it does not call a model inline.
**Context.** Panels deliver farmed counts, drop stats, and launch-failure diagnoses as PC screenshots (`docs/hermes/skills/01`, `03`); reading them is the one regex-intractable task.
**Consequences.** (+) Keeps the core free of any image/model dependency and degrades safely. (−) Vision-dependent metrics show `?` until the overseer resolves them.

---

## Effort / impact table

| Item | Phase | Effort | Impact | Notes |
|------|-------|--------|--------|-------|
| Concurrency test failures | Hotlist #1 | M | Medium | Real failures on pristine code; fix before building on `bot_interface` |
| Declare/optional-guard deps | Hotlist #2 | S | Medium | Sheets + GUI features silently unrunnable on clean install |
| Docstring + f-string nits | Hotlist #3–4 | S | Low | Cheap correctness |
| **Farm-stats parser** | **1** | **L** | **High** | Foundation for all AI removal; needs real samples |
| Deterministic report commands | 2 | L | High | Removes the recurring OpenRouter token cost |
| Deterministic triage (drop Ollama) | 3 | M | High | Removes per-message model call; fixes ordering drift |
| Deterministic novel-error handling | 4 | L | High | Removes last OpenRouter dep from the core |
| Hermes overseer endpoints | 5 | L | High | The seam that lets AI drive without re-coupling |
| Vision-on-demand via overseer | 6 | M | Medium | Optional; closes the screenshot gap |
