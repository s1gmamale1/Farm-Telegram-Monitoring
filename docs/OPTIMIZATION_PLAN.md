# WatcherDog / Hermes Optimization Plan

**Goal:** Make the watcher run *script-first, AI-last*. Scripts handle everything
they already know how to handle (classify, match, fix, report) with **zero LLM
calls**. The AI is invoked only when (a) the owner asks something, or (b) an
error is genuinely novel. Known fixes are applied **automatically** — never
"want me to fix?". Every hour, post what was auto-fixed.

This plan is tied to the current code. It does not rebuild anything that already
works (learned_fixes, self_edit, self_restart, daily_report, commands); it
*rewires* them so the LLM stops being on the hot path.

## Status

- ✅ **Phase 1** — tools live + startup `ACTIONS:` mode log (`run_watcher.py`).
- ✅ **Phase 2** — deterministic auto-fix router `watcherdog/auto_fix.py`, wired
  ahead of the AI in `_evaluate_bot`; `learned_fixes`/`save_fix` carry
  `action`/`auto`; known noise marked `action: ignore`.
- ✅ **Phase 3** — known fixes act (no text question); silence posts a relaunch
  card instead of a text "want me to restart?".
- ✅ **Phase 3.5** — `watcherdog/buttons.py` (signed single-use tokens) +
  `BotInterface.post_action_card`/`_on_callback`; confirm/relaunch cards
  tappable by anyone in the group, executed on the user account.
  - *Note:* the rare **novel-destructive** case still reaches the AI fallback,
    which asks in text (the model can't mint bot buttons itself). Deterministic
    paths (silence + known destructive `needs_confirm`) use buttons.
- ✅ **Phase 4** — hourly report appends a "🔧 Fixed last hour" line
  (`daily_report.summary_since`); "No fixes needed" when empty.
- ✅ **Phase 5** — `watcherdog/roster.py` (shared scan) + `watcherdog/fast_commands.py`:
  `/status /problems /silent /fixes /mode` answered with **no LLM** (bot + ibo
  paths); registered in the `/` menu. Drops/$-value commands stay on the AI.
- ✅ **Phase 6** — `docs/hermes/skills/07-self-improve.md` + `/improve <what>`;
  investigate→edit→validate→restart, admin/owner-gated (`can_edit` on both the
  bot-admin and ibo paths).
- ✅ **Phase 7** — slimmed `_PREAMBLE_ACTIONS` + skill 2 to the router reality
  (model = novel-error handler; always `save_fix` with a runnable `action`).

**All phases complete. 412 tests passing.**

---

## Root causes (why the issues happen today)

**One-line root cause:** the system was architected *AI-first* — the model is the
router, the fixer, **and** the command handler — with the deterministic pieces
(`classify`, `find_fix`, `tg_actions`, `commands`) sitting *behind* the model
instead of *in front* of it. Every fix below inverts that.

| Symptom | Exact cause (file:line) |
|---|---|
| Too many tokens | `_evaluate_bot` has **no deterministic branch** — every real error goes straight to the paid model: `mcp_watcher.py:278-279` calls `_incident_via_agent` → `agent.answer()`. The learned-fix lookup is *delegated to the model* (directive "call lookup_fix first", `mcp_watcher.py:213`), so the model must already be running to use the brain. A 100%-known error still costs an Ollama `analyze_message` (`mcp_watcher.py:255`) **plus** a full `agent.answer` turn (up to `AGENT_MAX_STEPS=12` rounds) with the whole 6-doc prompt (`run_watcher.py:86-88`) + growing `agent_history` resent each time. **The brain saves zero tokens** — it only changes what the already-running model decides. |
| "Want me to restart SF9 & SF10?" instead of fixing | **Prescribed by the system prompt**, not a bug: `run_watcher.py:59-64` — rule 3 "if DESTRUCTIVE (Kill/Restart/Reboot/Shutdown) … Ask ibo 'Want me to do this? (yes/no)'" and rule 4 "if NO saved fix — ask ibo". A relaunch is Kill/Start (destructive) **and** had no saved fix → both rules force a question. No `action:` mapping exists to run a relaunch automatically. |
| Tools in dry-run / blocked | Actions are double-gated (`agent.py:680-684`): `AGENT_ACTIONS_ENABLED` **and** per-call `execute`. The **bot front-end is always read-only** — `run_watcher.py:145` builds it with `actions=False` and `bot_interface` passes `execute=False`, so talking to the *bot* always returns `{"error":"dry-run: would act…"}`. `BOT_ACTIONS_ENABLED` also defaults `false` (`config.py:400`). Incident path uses `execute=deliver` (`mcp_watcher.py:224`), and `deliver = not args.dry_run` (`run_watcher.py:153`). |
| Hermes "not learning" | `save_fix` runs only **if the model chooses to** (`run_watcher.py:64` is an instruction, not a guarantee). No deterministic "owner taught me → persist it". And even saved, the next occurrence still invokes the model (see token row), so learning never removes the AI. |
| No self-improvement from a prompt | `BOT_SELF_EDIT_ENABLED` defaults **`false`** (`config.py:416`); self-edit tools need `can_edit` (`agent.py:696-698`) → the agent **literally cannot touch its own code**. No skill doc teaches the investigate→edit→restart loop; the action guide list (`run_watcher.py:86-88`) includes none. |
| Skills/scripts heavy | `commands.expand` (`mcp_watcher.py:386`) turns `/today`, `/weekly`, … into **LLM prompts** — each command is a paid agent turn (only `/help`, `/start` answer directly). The full 6-doc prompt is rebuilt + resent on every `agent.answer`. Hourly report has no "fixes this hour" line. |

---

## Design: the deterministic router (the core change)

Insert a **pre-AI router** in `mcp_watcher` that every farm message hits first.
No LLM call happens unless this router escalates.

```
farm message ──▶ classify()                         (scripts, no LLM)
                   │
          normal ──┴── error/unknown
                          │
                   learned_fixes.find_fix(text)      (scripts, no LLM)
                          │
              hit ────────┴──────── miss
               │                     │
   type=ai & non-destructive    novel error
               │                     │
        APPLY NOW via            escalate to AI ONCE:
        tg_actions               agent.answer() proposes a fix,
        (press_button/           applies it, then save_fix()
         send_command)           so next time is script-only
               │                     │
        daily_report.record() ◀──────┘   (both log the fix)
               │
        (no message to owner unless it failed)
```

Rules:
- **type=human** fix → do *not* auto-act; alert the owner (needs a person).
- **destructive** (Kill/Restart/Reboot/Shutdown) with a known fix → apply only if
  the fix is explicitly marked `auto: yes`; otherwise post a **confirm button**
  (see below) — never a text question.
- Owner reply that teaches a fix → `save_fix()` immediately (deterministic),
  so the same error never reaches the LLM again.

---

## Design: confirmation = inline buttons, openable by anyone in the group

Today the bot **asks in text** ("Want me to restart SF9? yes/no") and only `ibo`
can answer, in the user-account DM. New model:

- **No more text questions for confirmation.** Whenever an action needs a yes
  (destructive fix, or a novel fix the AI proposes), the **bot posts the message
  in the group with inline buttons under it**:

  ```
  ⚠️ SF9 — farm dead (device on). Proposed: relaunch (Kill All → Start 4).
  [ ✅ Do it ]   [ ✋ Skip ]   [ 🔁 Restart instead ]
  ```

  Built with the Bot API inline keyboard; presses arrive as **callback queries**
  the bot handles deterministically (no LLM to interpret a "yes"). Common
  one-tap actions (Relaunch, Kill+Start, Screenshot, Restart) get their own
  buttons so the owner rarely has to type at all.

- **Anyone in the group can press.** Per the owner's instruction, confirmation
  buttons are **not** restricted to `ibo`/`BOT_ACTION_USERS` — any member of the
  watch group can tap to execute. The button press itself is the authorization.
  - Implementation: the callback handler runs the mapped action regardless of
    presser id (gated only by group membership + the action being one the bot
    offered). The actor's name/id is recorded in the fix log + the result line
    ("✅ Relaunched SF9 — tapped by @someone").
  - **Safety kept minimal but present:** the callback payload is a signed/opaque
    token the bot generated for *that specific* pending action, so a button only
    ever does the exact thing it was posted for (no replaying / forging arbitrary
    actions). Buttons expire after a timeout and are disabled once pressed.

- The **bot front-end must be allowed to act** for this to work (it is read-only
  today — see Phase 1): callback-driven actions run through the user account's
  `tg_actions`, with `execute=True`.

---

## Phased execution

### Phase 1 — Enable tools (stop dry-run)  ·  *small, do first*
- Confirm `AGENT_ACTIONS_ENABLED=true` and live runs pass `deliver=True`
  (`run_watcher.py`). Add a startup log line stating **ACTIONS: LIVE / DRY-RUN**
  so the mode is never ambiguous again.
- Add `.env` keys (documented in `.env.example`): `AGENT_ACTIONS_ENABLED=true`,
  `BOT_ACTIONS_ENABLED=true` (owner-scoped), `BOT_SELF_EDIT_ENABLED=true`.
- **Accept:** startup log shows `ACTIONS: LIVE`; a known non-destructive fix
  actually presses the button (no dry-run error in logs).

### Phase 2 — Deterministic auto-fix router (the token fix)  ·  *core*
- New module `watcherdog/auto_fix.py`: `try_auto_fix(client, cfg, bot, text)`
  → uses `classify` + `learned_fixes.find_fix`; if a non-destructive `type=ai`
  fix matches, executes the mapped panel action(s) via `tg_actions`, calls
  `daily_report.record(...)`, and returns a result — **no `agent.answer`**.
- Rewire `_evaluate_bot`: call `auto_fix.try_auto_fix` first. Only on a miss
  (novel error) fall through to `_incident_via_agent` (one LLM call) which must
  end by calling `save_fix` so the next occurrence is script-only.
- Map fix text → concrete action: extend a learned-fix block with an optional
  `- action:` field (e.g. `Kill All; Start selected`) the router can execute
  literally. Free-text `fix:` stays human-readable.
- **Accept:** a repeated known error is fixed with **0 LLM tokens** (verify via
  logs: `AUTO-FIX <bot> … (no AI)`); novel error consults AI once then is added
  to the brain.

### Phase 3 — Auto-fix, don't ask (and never ask in text)  ·  *behavior*
- Remove the "Want me to…?" text pattern entirely: the router acts on known
  non-destructive fixes, then reports *what was done* (past tense).
- Anything that still needs a confirmation → **post an inline-button card**
  (Phase 3.5), never a typed yes/no. Owner is pinged live **only** when: a fix
  failed, the fix is `type=human`, or a destructive/novel action needs one tap.
- Apply the SF9/SF10 silence case: "device on, farm dead → re-launch" becomes a
  learned fix with an `action:` so silence-recovery is automatic.
- **Accept:** known issue produces "✅ Fixed SF9 — relaunched farm" (no question);
  anything needing a yes shows buttons, not text.

### Phase 3.5 — Inline confirm buttons, tappable by anyone in the group  ·  *UX*
- New module `watcherdog/buttons.py`: build inline-keyboard cards and handle the
  Bot API **callback queries**. Each pending action gets an opaque signed token
  (action id + target + nonce) embedded in the callback data; the handler looks
  it up, runs the mapped `tg_actions` call via the user account, edits the card
  to a result line, and disables the buttons. Tokens expire on a timeout.
- Wire into `bot_interface`: register a `CallbackQuery` handler; allow the bot
  front-end to *act* on a valid button press (lift the always-read-only gate for
  this path only). The presser's id/name is logged, not used to authorize —
  **any group member may tap** (owner's explicit choice).
- Standard button sets: per-incident `[✅ Do it][✋ Skip][🔁 Restart][📸 Shot]`;
  a `/panel <SFx>` card with the common one-tap actions so the owner can drive a
  panel without typing.
- Replace the incident/novel-fix "ask ibo in text" paths (`mcp_watcher.py`
  directive + `run_watcher.py` preamble rules 3–4) with "post a button card".
- **Accept:** an action needing confirmation appears as tappable buttons in the
  group; a non-owner member can tap `✅ Do it` and the action runs; the card
  updates to "✅ done — tapped by @x"; a stale/forged callback is rejected.

### Phase 4 — Hourly fix summary  ·  *reporting*
- Add `daily_report.summary_since(ts)` and inject a **"Fixed this hour"** block
  into the existing hourly report in `mcp_watcher`:
  `🔧 Last hour: SF3 — proxy timeout (relaunched), SF7 — CS frozen (killed+start)`.
- Keep the end-of-day rollup as-is.
- **Accept:** hourly post lists each auto-fix with PC + issue; empty hour says
  "no fixes needed".

### Phase 5 — Slash commands without AI  ·  *token + skills*
- Convert farm commands (`/today`, `/weekly`, `/top`, `/worst`, `/value`,
  `/problems`, `/silent`, `/check`, `/bans`) from LLM-prompt expansion to
  **deterministic handlers** that read `store` / drop-stats directly and format
  the reply. AI is used only when a command genuinely needs reasoning.
- Register the menu via `setMyCommands` (`BOT_SET_COMMANDS=true`) so they show in
  Telegram's `/` UI.
- Add `/fixes` (today's auto-fixes), `/mode` (LIVE vs dry-run + flags),
  `/improve <text>` (Phase 6).
- **Accept:** `/today` and `/problems` answer with **0 LLM tokens**; `/help`
  lists the full menu.

### Phase 6 — Self-improvement from a prompt  ·  *the "do what Claude does"*
- Add skill doc `docs/hermes/skills/07-self-improve.md`: when the owner sends an
  improvement request (or `/improve …`), the agent must (1) read the relevant
  skill/code with `list_project_files`/`read_project_file`, (2) make the change
  with `edit_project_file`/`apply_code_change` (auto-backed-up), (3) validate +
  `restart_watcher` to deploy. All admin-gated; destructive confirm rules stay.
- Gate behind `BOT_SELF_EDIT_ENABLED=true`, owner-only.
- **Accept:** sending "optimize your hourly report wording" causes a real,
  backed-up code edit + self-restart, with the change live after relaunch and a
  rollback path if the import check fails.

### Phase 7 — Skill/prompt slimming  ·  *steady-state token cost*
- Audit the system prompt + the 520 lines of `docs/hermes/skills/*`: the router
  now handles routine errors, so the model's prompt only needs the *novel-error*
  and *self-improve* paths. Trim redundant guidance; keep the brain
  (`learned_fixes.md`) as the durable knowledge, not the prompt.
- **Accept:** measured tokens-per-novel-incident drop vs. baseline; routine
  incidents cost zero.

---

## Acceptance criteria (whole plan)

1. A repeated, known error is detected and fixed with **no LLM call** (logs prove it).
2. A novel error consults the AI exactly **once**, applies a fix, and is saved to
   the brain so the next time is script-only.
3. The owner sees **actions taken** ("✅ Fixed SF9 — relaunched"); anything needing
   a yes is a **tappable inline button**, never a text question.
4. **Any group member** can tap a confirm button to execute it; the action is the
   exact one the card was posted for, and the presser is logged.
5. Tools run **live** (no dry-run errors) with a clear startup `ACTIONS: LIVE` line,
   including the bot front-end acting on button presses.
6. Each hour posts a **"Fixed this hour"** summary; end-of-day rollup unchanged.
7. Core slash commands answer **deterministically** (no AI) and appear in the `/` menu.
8. The owner can tell the bot to **improve its own code**, and it edits + restarts
   itself safely (validated, backed up, auto-rollback on failure).

## Out of scope / risks
- **Open group access is a deliberate loosening** (owner's instruction): any member
  of the watch group can trigger actions by tapping a button. Mitigations kept:
  buttons only do the one action they were posted for (opaque signed token, single
  use, expiring), every press is logged with the actor, and destructive buttons are
  visually distinct. If the group ever contains untrusted members, re-tighten via
  `BOT_ACTION_USERS`.
- Self-edit is owner-only and behind a flag; every write is backed up and the
  restart validates imports before swapping, with a supervisor rollback.
- Action→fix mapping (`action:` field) must be authored carefully so the router
  presses the right buttons; unmapped fixes fall back to the AI path, not a wrong action.
