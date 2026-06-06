# Design Spec — Deterministic Panel Monitoring & Recovery (#1 + #2)

- **Date:** 2026-06-07
- **Status:** Approved design — ready for implementation planning
- **Sub-projects covered:** #1 (detection + recovery rule engine) and #2 (Telegram action layer)
- **Related:** `docs/wiki/components/Monitoring and Recovery Rules.md`, `docs/wiki/reference/Panel Control Bot.md`, `ROADMAP.md`, memory `architecture-pivot-telegram-first-control`

---

## 1. Context & goal

WatcherDog runs on a real Telegram **user account** and already drives the SinFermera FSM-panel control bots via `watcherdog/tg_actions.py` (`press_button`, `panel_menu`, `send_command`, `screenshot`). Today the *decision* of what to do with a panel is made by an LLM (the OpenRouter agent). This spec replaces that decision with a **deterministic rules engine**, porting the on-PC `Watchdog.exe` recovery logic to operate over Telegram.

**Goal:** WatcherDog watches each panel from its Telegram status message and recovers the common failure modes by pressing the control bot's buttons — with **no model anywhere in the path**. Cold cases it physically cannot fix over Telegram are detected and flagged for a human (and, later, the per-PC API agent, sub-project #4).

## 2. Scope

**In scope**
- `farm_stats.py` — parse the panel status message into a typed `PanelStatus`.
- `panel_actions.py` — named, composable Telegram actions (the #2 action layer) over the existing `tg_actions`.
- `panel_rules.py` — the R1–R6 decision engine + constants + timing/debounce.
- Integration into the monitor loop (`mcp_watcher`) as a per-panel evaluation that replaces the AI incident path for panels.
- Confirm-gating for destructive presses (reusing `buttons.py`).
- A **cold-case seam**: R4/R6 produce a deterministic "needs per-PC API" alert (alert-only for now).
- Config keys + unit tests.

**Out of scope (separate sub-projects)**
- #3 deterministic report commands (`farm_stats` is built here and reused there).
- #4 the per-PC API agent (only the alert-only seam is built here) and the buggy RDP-host-restart fix.
- #5 the AI overseer.
- Removing Ollama/OpenRouter from the *rest* of WatcherDog (this spec only de-AI's the panel path).

## 3. Architecture

A per-panel evaluation runs each sweep in [[The Monitor Loop|`mcp_watcher`]]:

```
read latest status msg  ──▶ farm_stats.parse_panel_status() ──▶ PanelStatus
                                                                   │
                          panel_rules.decide(status, state, now, cfg)
                                                                   │
                          Decision{kind, actions, reason, destructive, cold_case}
                             │            │                 │
                          noop         sequence            flag (R4/R6)
                          (record)        │             cold-case alert
                                 ┌────────┴─────────┐
                          destructive?          non-destructive
                          confirm card        auto (if enabled) else confirm
                                 └──────── panel_actions.* ────────┘
```

Pure functions for parse and decide; all side effects isolated in `panel_actions`. Mirrors `Watchdog.exe` but over Telegram (no OCR — the panel already posts structured text).

## 4. Components

### 4.1 `watcherdog/farm_stats.py`
Pure parsing, fail-safe (never raises, never guesses; unknown fields → `None`).

```python
@dataclass
class Account:
    slot: int | None        # e.g. 54
    name: str | None        # "lilpro51"
    level: int | None
    xp: int | None

@dataclass
class PanelStatus:
    launched: int | None        # "Launched: N accounts"
    status: str | None          # "LIVE", etc.
    map: str | None
    score: str | None           # "[1:0]"
    in_match: bool              # True iff map+score present (and, for R3, advancing — see §6)
    accounts: list[Account]
    total: int | None           # "Total: N"
    updated_at: datetime.time | None   # "Updated: HH:MM:SS"
    raw: str

def parse_panel_status(text: str) -> PanelStatus            # returns PanelStatus with None fields on miss
def launched_from_alert(text: str) -> int | None           # "All 8 accounts launched!" -> 8
```

Parsing approach: line-based with per-field regex keyed off the labels in [[Panel Control Bot]] (`Launched:`, `Status:`, `Map:`, `Score:`, `Total:`, `Updated:`, account lines). Emoji-tolerant.

### 4.2 `watcherdog/panel_actions.py`  (the #2 action layer)
Async, built on the existing `tg_tools`/`tg_actions`. Every button is resolved by a **distinguishing matcher** read live from the inline keyboard — never a hard-coded index.

```python
# atomic actions (each returns ActionResult{ok, detail})
async def screenshot(client, panel, *, execute) -> ActionResult        # downloads the photo
async def kill_all(client, panel, *, execute) -> ActionResult          # "Kill all CS & Steam"  (destructive)
async def select_unfarmed(client, panel, *, execute) -> ActionResult   # "Select 4/10 unfarmed"  (NOT "Select first 4/10 accs")
async def start_selected(client, panel, *, execute) -> ActionResult    # "Start selected accounts"
async def make_lobbies(client, panel, *, execute) -> ActionResult      # "Make lobbies and search game"
async def drop_stats(client, panel, *, execute) -> ActionResult        # "Drop Stats"
async def run_activity_booster(client, panel, *, execute) -> ActionResult
async def restart_panel(client, panel, *, execute) -> ActionResult     # destructive
async def reboot_pc / shutdown_pc(...)                                 # destructive

# composed sequences (with settle waits between steps)
async def relaunch_four(client, panel, *, execute)   # R1: kill_all -> wait -> select_unfarmed -> wait -> start_selected
async def restore_four(client, panel, *, execute)    # R2: select_unfarmed -> wait -> start_selected

# helpers
def screenshot_is_black(image_bytes) -> bool          # deterministic mean-pixel threshold; how R4 is detected (no vision model)
```

**Matcher discipline (critical):** `select_unfarmed` matches the label containing `unfarmed`; it must NEVER match `Select first 4/10 accs`. Reuse the existing exact/prefix logic in `tg_actions.press_button` (already covered by `test_tg_actions::test_press_button_exact_match_wins_over_prefix`).

**Settle waits:** configurable per-step waits between sequence steps (default ~3–5 s, mirroring `Watchdog.exe`'s `settle_after_click_ms`), so a `Start` doesn't fire before `Kill all` finishes.

### 4.3 `watcherdog/panel_rules.py`  (the #1 engine)

```python
@dataclass
class PanelState:
    over_launch_since: float | None
    idle_since: float | None
    last_action_ts: float | None
    last_score: str | None
    last_score_ts: float | None

@dataclass
class Decision:
    kind: str                 # "noop" | "sequence" | "flag"
    actions: list[str]        # e.g. ["kill_all", "select_unfarmed", "start_selected"]
    reason: str
    destructive: bool
    cold_case: bool           # True for R4/R6 (handled by alert-only seam)

def decide(status: PanelStatus, state: PanelState, now: float, cfg) -> Decision
```

`decide()` encodes R1–R6 (§5) plus debounce and the over-launch/idle persistence windows. Pure (no I/O), fully unit-testable.

## 5. The rules (authoritative)

Source of truth: [[Monitoring and Recovery Rules]]. Precise form:

| # | Trigger | Decision | Destructive? | Cold case? |
|---|---------|----------|:---:|:---:|
| R1 | `launched > target` continuously for `> overlaunch_minutes` | `sequence` = relaunch_four (kill_all → select_unfarmed → start_selected) | **yes** | no |
| R2 | `launched < target` **or** status not LIVE (and not a cold case) | `sequence` = restore_four (select_unfarmed → start_selected) | no | no |
| R3 | status LIVE, `launched == target`, but **not farming** (no map/score, or score unchanged for `idle_minutes`) | `sequence` = [make_lobbies] | no | no |
| R4 | accounts won't launch (R2 attempted, still wrong after debounce) **and** `screenshot_is_black()` | `flag` cold_case "RDP host bugged — restart needed" | n/a | **yes** |
| R5 | scheduled: Wednesday 00:00 | `sequence` = [kill_all, drop_stats, run_activity_booster] (ensure 0 launched first) | yes (kill) | no |
| R6 | status stale (`updated_at` older than `stale_minutes`) or bot unreachable | `flag` cold_case "panel/PC down — relaunch/reboot needed" | n/a | **yes** |
| — | `launched == target`, LIVE, farming | `noop` | — | — |

Constants (config, §8): `target=4`, `overlaunch_minutes=15`, `idle_minutes` (tune), `stale_minutes`, `action_debounce_seconds=180`.

**Evaluation precedence** (first match wins, so a dead panel never gets a pointless `restore_four`): **R6** (stale/unreachable) → **R4** (R2 has failed repeatedly *and* screenshot is black) → **R1** (over-launch) → **R2** (under-launch / not LIVE) → **R3** (idle) → `noop`. R4 is a follow-up state: it only arms after R2 has run and the panel is still wrong past `action_debounce_seconds`.

R5 is driven by the existing weekly scheduler in `mcp_watcher`, not per-sweep `decide()`; it calls the same `panel_actions`.

## 6. Detection strategy (Hybrid — approved)

- **Passive each sweep:** parse the auto-updating status message. No button press → no flood across 24 panels.
- **Active confirm only when needed:** before a destructive action (R1) or when the status is stale/ambiguous, press `Screenshot` (and/or `Launched accs stats`) to confirm before acting. This is also how R4's black-image is obtained.
- **R3 "farming" signal:** `in_match` = map+score present. To distinguish "in a match" from "stuck on a match that isn't progressing," track `last_score`/`last_score_ts`; if score is unchanged for `idle_minutes`, treat as idle → R3. (Flagged as the rule most likely to need live tuning.)

## 7. Recovery execution & confirm-gating

Decision dispatch in the loop:
- `noop` → record only.
- `flag` (cold_case) → deterministic alert to ibo ("🧰 Panel needs per-PC API: <reason>"); recorded; **no model**.
- `sequence`:
  - **destructive** (R1, R5-kill, restart/reboot/shutdown) → **always post a [[Confirm and Action Buttons|confirm card]]**; the sequence runs only on tap, **unless** `PANEL_AUTO_DESTRUCTIVE=true` (default **false**).
  - **non-destructive** (R2, R3) → run automatically when `PANEL_AUTO_RECOVER=true` (default **true**); otherwise post a confirm card.

> **Resolved open question (`PANEL_AUTO_RECOVER`):** default **true** for non-destructive recoveries (R2/R3 are safe — selecting/starting/lobbies), and destructive recoveries (R1 kill-all) default to **confirm-required** (`PANEL_AUTO_DESTRUCTIVE=false`). The operator can flip kill-all to fully automatic once trusted. This keeps the dangerous lever human-gated by default while still auto-healing the safe cases.

All execution respects `--dry-run`/`deliver` and a master `PANEL_RULES_ENABLED`. Debounce blocks repeating the same action on a panel within `action_debounce_seconds`.

## 8. Configuration (new `.env` keys)

| Key | Default | Meaning |
|-----|---------|---------|
| `PANEL_RULES_ENABLED` | `true` | Master switch for the deterministic panel engine |
| `PANEL_TARGET_ACCOUNTS` | `4` | Required launched-account count |
| `PANEL_OVERLAUNCH_MINUTES` | `15` | Persistence before R1 fires |
| `PANEL_IDLE_MINUTES` | `10` | Score-unchanged window for R3 |
| `PANEL_STALE_MINUTES` | `30` | `Updated` age before R6 |
| `PANEL_ACTION_DEBOUNCE_SECONDS` | `180` | Min gap between actions on a panel |
| `PANEL_AUTO_RECOVER` | `true` | Auto-run non-destructive recoveries (else confirm) |
| `PANEL_AUTO_DESTRUCTIVE` | `false` | Auto-run destructive recoveries (else confirm) |
| `PANEL_SETTLE_SECONDS` | `4` | Wait between sequence steps |

Weekly Drop Stats reuses existing Wednesday-00:00 job config.

## 9. Error handling & edge cases

- **FloodWaitError** → back off, skip the panel this sweep; never tight-loop presses.
- **Button label not found** (label drift) → `flag` for human, do **not** guess a different button.
- **Parse failure / partial status** → treat fields as unknown; never take a destructive action on a misread — active-confirm (`Screenshot`) first, else `flag`.
- **Black screenshot** → R4 cold-case flag.
- **Stale `Updated`** → R6 (don't act blindly on stale data).
- **Restart mid-action** → per-panel timers reset (anti-false-positive, like `Watchdog.exe`); in-flight confirm cards persist via existing `task_store`.
- **Dry-run / disabled** → decide + log, no press.

## 10. Logging / telemetry

Each decision + executed action recorded to the existing auto-fix log (`daily_report` / `daily_errors.jsonl`) so the hourly "🔧 Fixed last hour" and daily rollups include panel recoveries. Cold-case flags counted distinctly.

## 11. Testing strategy

- `tests/test_farm_stats.py` — the **real status message** (from the operator's screenshot) as the golden fixture; variants: over-launch alert, missing map/score, stale `Updated`, partial/garbage text → all parse safely.
- `tests/test_panel_rules.py` — each R1–R6 with synthetic `PanelStatus` + `PanelState` + clock → expected `Decision`; debounce; over-launch 15-min persistence; idle score-unchanged; stale → R6.
- `tests/test_panel_actions.py` — button resolution incl. **unfarmed-vs-first disambiguation**; `screenshot_is_black` on black/non-black bytes; sequence composition + settle waits (mock client records calls); destructive → confirm-gated, not pressed. No network, no model.

## 12. Rollout / validation plan

1. **Dry-run** against the live folder: `decide()` logs what it *would* do for every panel, no presses. Verify decisions match reality for ≥1 full day.
2. **One supervised live panel:** enable for a single `SinFermeraN`, watch R1/R2/R3 fire correctly (and confirm timing/settle waits).
3. **Fleet rollout** once the single panel is solid. R4/R6 remain alert-only until #4 ships.

## 13. Open assumptions (to validate with real data)

- Only one status-message format sampled; broken/offline-panel formats unverified → parser is fail-safe but needs 2–3 more real samples (esp. a broken panel).
- R3 "not farming" signal (score-unchanged) is a hypothesis; may need tuning against live behavior.
- Settle waits between sequence steps are estimates pending a live run.

## 14. Definition of done

- A panel with `launched > 4` for >15 min produces a confirm-gated `kill_all → select_unfarmed → start_selected`, **no model call**.
- `launched < 4` / not LIVE → auto `select_unfarmed → start_selected` (when `PANEL_AUTO_RECOVER`).
- LIVE but idle → `make_lobbies`.
- Black screenshot / stale panel → deterministic "needs per-PC API" flag (no model).
- `farm_stats`, `panel_rules`, `panel_actions` unit-tested against real-message fixtures; full suite green.
- **No AI import on the panel evaluation path.**
