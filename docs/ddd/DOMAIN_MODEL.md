# WatcherDog — Domain Model (DDD)

Bounded contexts of the Farm-Telegram-Monitoring system (`watcherdog/`), the
deterministic Telegram-first brain that watches the 24 SinFermera CS2/Steam farm
panels, classifies their health, recovers them, and reports to the owner (`ibo`).

> Ubiquitous language: **panel** (a SinFermera bot/PC), **bucket** (FARMING /
> QUIET / ATTENTION / DEAD), **freshness** (age of a panel's last message),
> **episode** (one recovery attempt-chain), **cold case** (needs-PC, unrecoverable
> from Telegram), **ibo** (the owner).

## Core contexts

### 1. Monitoring & Orchestration  `(core)`
The deterministic run loop — the brain. Sweeps the `Farms` folder every 120 s,
schedules hourly/daily/weekly reports, drives the recovery ladder, emits the
heartbeat beacon, and performs safe self-restart.
- Modules: `mcp_watcher.py`, `monitor.py`, `heartbeat.py`, `self_restart.py`
- Invariants: at most one report per clock hour; recovery keys on freshness/liveness.

### 2. Health Classification  `(core)`
Deterministic, **NO-LLM** bucketing of each panel from its latest message +
freshness into FARMING / QUIET / ATTENTION / DEAD. Flags on **freshness, not
content** — fresh "lobby creation"/"launching" chatter is healthy; only genuine
errors, wrong account count, staleness (>90 m), or death (>180 m) flag red.
- Modules: `classifier.py`, `roster.py`, `analyzer.py`
- Language: error/normal/unknown bucket, `_FARMING_KEYWORDS`, account count, silence self-report.

### 3. Recovery & Panel Control  `(core)`
The R1–R6 recovery rules: button presses, relaunch (Select 4 → Start),
match-search handling, RDP-reboot rung, retry-cap → cold case, and the
learned-fix router that auto-applies known fixes before any model runs.
- Modules: `panel_actions.py`, `panel_rules.py`, `buttons.py`, `auto_fix.py`,
  `novel_recovery.py`, `learned_fixes.py`, `restart_helper.py`
- Gate: `OVERSEER_ALLOW_DESTRUCTIVE` / `PANEL_AUTO_DESTRUCTIVE` for destructive steps.

## Supporting contexts

### 4. Reporting  `(supporting)`
Pure-function, status-grouped reports + fast slash-commands. Unit-testable over
plain dicts; no Telethon, no model.
- Modules: `hourly_report.py`, `daily_report.py`, `fleet_report.py`,
  `fast_commands.py`, `commands.py`  (commands: `/status` `/problems` `/silent` `/fleet`)

### 5. Incident & State  `(supporting)`
SQLite incident ledger (open / resolve / dedup by error-hash), persisted runtime
state, and the task queue.
- Modules: `incident_tracker.py`, `storage.py`, `task_store.py`  (DB: `data/incidents.db`)

### 6. Alerting  `(supporting)`
Severity-gated owner alerts and incident escalation / resolution / follow-up
message formatting + delivery (prefers bot DM, falls back to user account).
- Modules: `alerter.py`

### 7. Stats & Drops  `(supporting)`
Weekly drop-stats pipeline, sheet export, and farm totals.
- Modules: `drop_stats.py`, `drop_sheets.py`, `farm_stats.py`

### 8. Overseer Interface  `(supporting)`
The **opt-in** AI overseer surface (9 endpoints) and the Hermes bridge — the only
place AI touches the system after the deterministic-core pivot (ADR-001).
- Modules: `overseer_api.py`, `hermes_bridge.py`  (+ `scripts/overseer_health.py`)

### 9. Agent  `(supporting)`
The read-only deepseek/OpenRouter agent that answers `ibo`'s questions using
Telegram read tools. Strictly observer; cannot drive panels.
- Modules: `agent.py`

## Infrastructure & shared

### 10. Telegram I/O  `(infrastructure)`
Telethon (MTProto) read/act layer: latest-message reads, panel-menu navigation,
button presses, allow-list, bot front-end.
- Modules: `tg_tools.py`, `tg_actions.py`, `telegram_source.py`, `bot_access.py`,
  `bot_interface.py`, `bot_logging.py`

### 11. Shared Kernel
Env-driven configuration consumed by every context (thresholds:
`SILENCE_THRESHOLD_MINUTES`=120, `QUIET_THRESHOLD_MINUTES`=60, stale 90 m, dead 180 m).
- Module: `config.py`

> Legacy (not a live context): `gui_mac.py` — the old screenshot+OCR GUI mode,
> replaced by the MTProto watcher.

## Context map (relationships)

```
            Shared Kernel (config.py)  ── underpins all ──┐
                                                          │
  Monitoring&Orchestration ──drives──> Health Classification ──feeds──> Reporting
        │                                     │
        ├──drives──> Recovery&Panel Control ──acts via──> Telegram I/O
        │                   │
        │                   └──records──> Incident&State ──surfaces──> Overseer Interface
        └──raises──> Alerting                                   Agent (read-only Q&A)
```

- **Monitoring** orchestrates Classification → Recovery → Alerting/Reporting.
- **Classification** is the shared roster consumed by both Reporting and Recovery.
- **Recovery** acts through Telegram I/O and records outcomes in Incident & State.
- **Overseer Interface** reads Incident & State + logs (opt-in AI); the deterministic
  core never calls a model.
