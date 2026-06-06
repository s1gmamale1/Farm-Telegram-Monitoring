---
title: Testing
tags:
  - watcherdog
  - operations
  - testing
updated: 2026-06-06
status: current
---

# Testing

> The pytest suite — **402 test functions across 31 `test_*.py` files** (32 `.py` files incl. `conftest.py`) — and how to run it after any change.

Part of [[Home]].

WatcherDog ships a regression suite under `tests/`. Run it after every change to [[The Monitor Loop|the loop]], [[The Agent|the agent]], or any module — see the build/test rule in [[Running WatcherDog]].

> [!info] Test count history (now correct)
> [[README]]/[[DOCUMENTATION]] once claimed *412 tests*; the verified count when this vault was first written was **302**. The suite was then expanded by **100** reviewed tests — notably new coverage for [[Telegram Tools and Actions|tg_tools]], `hermes_bridge`, and `bot_logging` — bringing it to **402 `def test_` functions across 31 `test_*.py` files** (plus `conftest.py`; `tests/README.md` is non-code). The root docs are now corrected to match.

> [!warning] 4 known failures in a clean checkout
> A full `pytest` run reports **511 passed, 4 failed** (parametrization expands 402 functions into 515 cases). The 4 failures are **not** logic regressions: 2 are the legacy GUI smoke imports (`watcherdog.gui_mac`, `run_gui`) that need the unpinned `pyobjc`/`Quartz` ([[Legacy Modes]]), and 2 are pre-existing concurrency assertions in `test_bot_interface.py` (`test_action_turns_serialize`, `test_stopjobs_...`) that also fail in isolation on pristine code.

## Running the suite

```
scripts/run_tests.sh                # run everything
scripts/run_tests.sh -k config      # only tests matching "config"
.venv/bin/python -m pytest          # direct
.venv/bin/python -m pytest tests/test_auto_fix.py   # one module
```

`scripts/run_tests.sh` `cd`s to the project root, prefers `.venv/bin/python` (falls back to `python3`), auto-installs `requirements-dev.txt` if `pytest` is missing, then `exec`s `pytest` passing through any args.

> [!tip] The dev dependency is just pytest
> `requirements-dev.txt` adds only `pytest>=8.0` on top of `requirements.txt` (which pins only `telethon>=1.36`). The whole suite runs without Ollama, OpenRouter, or a live Telegram — it stubs those boundaries.

## The 31 test modules

> [!tip] ⭐ marks the three modules added this session (new coverage for previously-untested code).

| Module | tests | Covers |
|--------|------:|--------|
| `test_commands.py` | 32 | [[Commands]] — fast/AI/meta layers, expand, static_reply |
| `test_drop_stats.py` | 27 | [[Drop-Stats Pipeline]] — iso_week, parse, buffer, schedule |
| `test_bot_access.py` | 26 | grant/revoke/granted_ids access list |
| `test_alerter.py` | 23 | [[Alerts and Heartbeat]] — formatting + send sinks + 429/5xx retry |
| ⭐ `test_tg_tools.py` | 20 | [[Telegram Tools and Actions]] — filter_title, entity_name, _chat_ref, latest_message error-swallow |
| `test_daily_report.py` | 18 | [[Scheduled Reports]] — jsonl log, hourly/daily summaries |
| `test_bot_interface.py` | 18 | [[The Bot Front-End]] — routing, capabilities, callbacks |
| ⭐ `test_hermes_bridge.py` | 17 | [[Legacy Modes\|hermes_bridge]] — layout math, ask_hermes failure modes |
| `test_config.py` | 17 | [[Configuration]] — env parse, path resolve, validators |
| `test_auto_fix.py` | 17 | [[Script-First AI-Last]] — the deterministic router |
| `test_storage.py` | 16 | [[Data and State\|IncidentStore]] — dedupe + recurring order |
| `test_tg_actions.py` | 15 | [[Telegram Tools and Actions]] — press_button exact/prefix, is_destructive |
| `test_monitor.py` | 14 | [[Legacy Modes\|LogMonitor]] + error_hash/normalize_error, logrotate |
| `test_learned_fixes.py` | 14 | [[The Learned-Fixes Brain]] — parse, find_fix, append_fix |
| `test_buttons.py` | 12 | [[Confirm and Action Buttons]] — signed token model |
| `test_analyzer.py` | 12 | [[Script-First AI-Last\|Ollama analyzer]] (+ _fallback, truncation) |
| `test_heartbeat.py` | 11 | [[Alerts and Heartbeat\|HeartbeatMonitor]] (legacy path) |
| `test_fast_commands.py` | 11 | fast command handlers |
| `test_classifier.py` | 10 | [[Script-First AI-Last\|classify()]] error/normal/unknown |
| `test_roster.py` | 9 | [[Roster and Health Scan]] — scan + classify_status |
| ⭐ `test_bot_logging.py` | 9 | [[Legacy Modes\|bot_logging]] — install(), excepthook wiring |
| `test_agent_dispatch.py` | 9 | [[The Agent]] — fan-out / dispatch |
| `test_self_restart.py` | 8 | [[Safe Self-Restart]] — pre-flight + rollback |
| `test_mcp_report.py` | 7 | [[The Monitor Loop]] reporting helpers |
| `test_fanout.py` | 7 | sub-agent fan-out concurrency |
| `test_task_store.py` | 5 | [[The Bot Front-End\|task_store]] persistence/resume |
| `test_run.py` | 4 | [[Legacy Modes\|run.py]] log-mode pipeline |
| `test_hourly_report.py` | 4 | [[Scheduled Reports\|hourly report]] guard |
| `test_card_routing.py` | 4 | [[Confirm and Action Buttons\|card]] routing |
| `test_agent_progress.py` | 4 | live progress bars |
| `test_smoke.py` | 2 | import + boot smoke test (2 GUI imports fail w/o `pyobjc`) |

> [!info] `conftest.py` is the 32nd file
> `tests/` holds 31 `test_*.py` modules plus `conftest.py` (the 32nd `.py` file, which just puts the project root on `sys.path`). `tests/README.md` is documentation, not code.

## What the tests exercise

The suite is unit-heavy and dependency-light by design — most modules are pure stdlib or stub the network:

- **Token-free stages** ([[Script-First AI-Last]]): `classify()` and `find_fix()` touch no model, so `test_classifier`/`test_learned_fixes` run fully offline.
- **Telethon-free button model** ([[Confirm and Action Buttons]]): `ActionRegistry` is deliberately Telethon-free, so `test_buttons`/`test_card_routing` verify HMAC signing, single-use consume, and ttl expiry without a client.
- **Atomic JSON stores**: `task_store` and `bot_access` use temp-file + `os.replace`, tested for concurrent-safe writes.

> [!tip] Two helpers for manual / integration testing
> Beyond pytest, `tools/demo_learned_fix.py` demos the learn-once-then-auto behavior (temp brain, `execute=False`) and `tools/simulate_error.py` writes a fake traceback/ERROR line into `logs/<bot>.log` to exercise the legacy `run.py` detect→analyze→alert pipeline. See [[Entry Points]] and [[Legacy Modes]].

## Verifying a change

```mermaid
flowchart LR
  A[edit code] --> B[scripts/run_tests.sh]
  B -- green --> C[run_watcher.py --dry-run]
  C -- looks right --> D[run live]
  B -- red --> E[fix, see Troubleshooting]
```

After code changes: run the suite, then a `--dry-run` sweep ([[Running WatcherDog]]) before going live. For failures, see [[Troubleshooting]].

## See also
- [[Running WatcherDog]] — the `--dry-run` step that pairs with the suite
- [[Troubleshooting]] — diagnosing failures the tests surface
- [[Script-First AI-Last]] — the token-free stages with the heaviest coverage
- [[Module Reference]] — the modules each test file maps to
- [[Entry Points]] — `tools/` probes used for manual testing
- [[Home]] — knowledge-base index
