# Hermes Overseer Ergonomics — Design

**Date:** 2026-06-14
**Status:** Approved (brainstorming)
**Track:** Overseer surface (consumer = the external Hermes agent; see [[deterministic-core-track-status]])

## Problem

The deterministic core is complete and the opt-in overseer socket surface exists
(`watcherdog/overseer_api.py`, 9 endpoints; `scripts/overseer_cli.py`). The owner
now wants to actually run an **external Hermes agent** (provider `openai-codex`,
`gpt-5.3-codex-spark`) as the "agent watching the watcher": it should cheaply see
whether WatcherDog is healthy, read the issues, and drive the overseer button APIs
to recover — **woken only when something is wrong** (the owner's "Option B": a cheap
host-side healthcheck wakes the expensive agent only on trouble).

Three concrete blockers were found:
1. **No way to see "is the watcher even alive."** `get_stats` runs *over the socket*
   (and does an expensive live button-sweep), but the socket dies with the process —
   so the single most important signal (watcher down) is exactly the one a
   socket call can't deliver.
2. **Stale launchd plist.** `com.watcherdog.telegram.plist` points at
   `/Users/macmini4/Documents/WatcherDogBot` (a different machine), so launchd can't
   keep this deploy alive — Option B's foundation is missing.
3. **`tools/` import collision.** `tests/test_tg_login.py` does `from tools import
   tg_login`; the repo's `tools/` has no `__init__.py`, and `tests/conftest.py` only
   prepends the repo root `if ROOT not in sys.path`. Under Hermes
   (`PYTHONPATH=~/.hermes/hermes-agent`, which has its own `tools` package), the root
   is present-but-not-first, the guard skips, and Python resolves Hermes's `tools` →
   `ImportError: cannot import name 'tg_login'`. Hermes can't run this repo's tests.

Plus a **novel risk class**: letting an LLM (Hermes) drive `press_button` / `run_ladder`
over the socket means an LLM can trigger destructive host actions (Kill-all / Reboot).

## Goal

Make the WatcherDog **repo** expose exactly what an external overseer needs — a
compact health/trigger view that works even when the watcher is down, a launchd
plist that keeps it alive, a clean import path, a repo-enforced destructive-action
guardrail, and a runbook — so the owner/Hermes can wire the actual agent loop
host-side with no further repo changes.

## Scope

**In scope (this repo, versioned):** the five components below.
**Out of scope (host-side, owner/Hermes wires up):** the `hermes chat` invocation,
its launchd/cron schedule, `~/.hermes` config, the Sigma-Agents skill store. The
repo provides primitives; the host composes them.
**Explicitly out of scope:** the talker bot (`TELEGRAM_BOT_TOKEN`) — that's the
in-Telegram command/alert front-end, orthogonal to the socket overseer; already
added to `.env`, live on the next restart. The health script does not check it.

## Decisions (locked during brainstorming)

| Question | Decision |
|---|---|
| Repo vs host boundary | Repo ships primitives only; `hermes chat` call is host-side |
| Wake triggers (nonzero exit) | **process dead/wedged** OR **flagged incidents present** |
| Report-only context (no wake) | recent errors/tracebacks, socket presence, last sweep, fleet |
| Action safety | Overseer safe-mode default: read/diagnose/teach free; **destructive refused unless `OVERSEER_ALLOW_DESTRUCTIVE=true`** |
| Health output | JSON to stdout + exit code |
| "Wedged" threshold | beacon older than **5× `watch_poll_interval`** (default 600 s) |
| Bot token | out of scope |
| Health-script data sourcing | **local-only** (no socket dependency); socket checked for existence only |

## Architecture

Five focused, independent pieces. No change to the deterministic monitor loop.

```
HOST (owner/Hermes, not versioned):
  launchd: KeepAlive WatcherDog  ─────────────► run_watcher.py (deterministic core)
  launchd/cron every ~1-2 min:
     scripts/overseer_health.py ── exit 0 ──► (silent, healthy)
                                └─ exit ≠0 ──► hermes chat -m gpt-5.3-codex-spark …
                                                   │ reads the JSON context
                                                   ▼
                                          scripts/overseer_cli.py <method> …
                                                   │ over OVERSEER_SOCKET
                                                   ▼
                                          watcherdog/overseer_api.py (safe-mode gated)
```

### 1. `scripts/overseer_health.py` — standalone health/trigger primitive

Pure-ish, **no socket dependency** (the watcher being down is the case that matters).
Reads:
- `process_alive` — is a `run_watcher.py` process running (scan via `pgrep`-equivalent
  / `psutil`-free `subprocess`; match the command line, exclude self).
- `beacon_age_s` — `now - mtime(cfg.watcher_health_path)` (the `data/watcher_healthy`
  beacon `self_restart.mark_healthy` writes). `wedged = process_alive and
  beacon_age_s > 5 * cfg.watch_poll_interval`.
- `flagged` — `IncidentTracker(cfg.db_path).novel_list()` read **directly from
  SQLite** (works when the watcher is down): `{"count": N, "bots": [...]}`.
- `last_sweep` — newest `Sweep: N chats, M healthy` line in `cfg` gui log
  (`data/gui_run.log`), rendered `"HH:MM (N chats, M healthy)"` or `null`.
- `recent_errors` — last ≤5 `Traceback`/`ERROR`/`CRITICAL` lines from
  `data/telegram.err.log` + the gui log (deduped, newest first), bounded length.
- `socket_present` — `os.path.exists(cfg.overseer_socket)` when configured, else null.

Emits a single JSON object to stdout:
```json
{"healthy": false, "process_alive": true, "beacon_age_s": 720, "wedged": true,
 "flagged": {"count": 1, "bots": ["SinFermera15"]},
 "last_sweep": "23:51 (24 chats, 19 healthy)",
 "recent_errors": ["2026-06-13 04:30 ConnectionError ..."],
 "socket_present": true}
```
**Exit code:** `0` when healthy; **nonzero (1)** when a wake-trigger holds —
`(not process_alive) or wedged or flagged.count > 0`. `healthy` in the JSON mirrors
`exit == 0`. The two report-only signals (recent_errors, socket_present) never flip
the exit code.

Config is loaded the normal way (`watcherdog.config`), so paths/intervals come from
the same `.env`. A `--json`/`--pretty` flag is unnecessary (JSON is the only output);
a `--quiet` is unnecessary (callers read the exit code). YAGNI.

### 2. `com.watcherdog.telegram.plist` — fixed

Rewrite to this deploy: `ProgramArguments` =
`/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python` +
`…/run_watcher.py` + `--verbose`; `WorkingDirectory` = the repo;
`StandardOutPath`/`StandardErrorPath` = `data/telegram.out.log` / `data/telegram.err.log`;
`KeepAlive` = true (launchd resurrects a crashed watcher — Option B's spine);
`RunAtLoad` = true. The `launchctl load` install command lives in the runbook
(component 5), not executed by this work.

### 3. `tools/` import-collision fix

- `tests/conftest.py`: change the guard so the repo root is forced to `sys.path[0]`
  **unconditionally** — if `ROOT` is already present elsewhere in `sys.path`, remove
  it and re-insert at index 0, so an external `PYTHONPATH` entry can never shadow the
  repo's `tools`/`watcherdog`.
- Add `tools/__init__.py` (empty, or a one-line docstring) making `tools` an explicit
  regular package for non-pytest invocations.
- The runbook documents the `PYTHONPATH= .venv/bin/python -m pytest …` form for Hermes
  one-shots as belt-and-suspenders.

Existing `from tools import tg_login` keeps working (submodule import of a regular
package).

### 4. Overseer safe-mode gate (the destructive guardrail)

- New config `overseer_allow_destructive` in `watcherdog/config.py`, parsed with the
  same inline truthy pattern the other bool configs use:
  `get("OVERSEER_ALLOW_DESTRUCTIVE", "false").strip().lower() in ("1", "true", "yes")`
  (default **false**), placed beside `overseer_socket`/`overseer_token`.
- **Gate on the MATCHED label, not the param.** `tg_actions.press_button` already
  resolves the request to a real button `label` before clicking (and an audit comment
  notes the param can understate what gets pressed — e.g. `"all cs"` → `"Kill All CS &
  Steam"`). A param-level check in the handler would therefore leak. Instead add an
  `allow_destructive=True` keyword to `press_button`: after `label, btn = match`, if
  `is_destructive(label) and not allow_destructive`, return
  `{"refused": "destructive", "button": label, "message": "...OVERSEER_ALLOW_DESTRUCTIVE
  is off"}` **before** clicking. Default `True` preserves every in-process caller
  (monitor loop, auto_fix) unchanged.
- `overseer_api._h_press_button`: pass
  `allow_destructive=getattr(cfg, "overseer_allow_destructive", False)` into
  `press_button` — so the overseer path is gated while the core path is not.
- `overseer_api._h_run_ladder`: the kill→select→start ladder is wholesale destructive
  and `novel_recovery.attempt` has no per-label notion, so gate at the handler: if
  **not** `getattr(cfg, "overseer_allow_destructive", False)`, raise
  `ValueError("run_ladder is destructive: set OVERSEER_ALLOW_DESTRUCTIVE=true to
  authorize")` **before** calling `attempt`.
- Untouched (always available): `list_flagged`, `read_bot`, `list_buttons`,
  `screenshot`, `get_stats`, `resolve_flagged`, `teach_fix` (which already refuses
  `auto:yes`+destructive). The owner-authorized in-process RDP auto-reboot uses a
  separate `tg_actions.press_button_then_confirm` (not `press_button`), so this gate
  does NOT touch `PANEL_AUTO_DESTRUCTIVE` behavior.
- Refusals surface as a normal result/error over the socket. Existing overseer tests
  that monkeypatch `press_button` must add `allow_destructive=True` to their fake
  signatures (else `TypeError` on the new kwarg); the `run_ladder` gate test must set
  `overseer_allow_destructive=True` in its `cfg` to reach `attempt`.

**Separation of concerns (must not be conflated):** `PANEL_AUTO_DESTRUCTIVE` governs
the *in-process core's own* owner-authorized auto-recovery (the panel ladder + RDP
reboot) and stays **default-true**. `OVERSEER_ALLOW_DESTRUCTIVE` is a *separate* gate
on the *external Hermes-driven* socket path and is **default-false**. The core keeps
fixing things autonomously; an external LLM must be explicitly granted hands.

### 5. `docs/wiki/reference/Hermes Overseer Runbook.md`

The consumer guide (host-side wiring the repo intentionally does not own):
- launchd install (the fixed plist), and a sketch of the healthcheck →
  `overseer_health.py` → on nonzero exit `hermes chat --provider openai-codex
  -m gpt-5.3-codex-spark --toolsets terminal,file,vision -q "<context>"`.
- the strict overseer prompt (prefer diagnosis; overseer API over raw Telegram;
  no destructive press unless `OVERSEER_ALLOW_DESTRUCTIVE`; on code edit run
  `py_compile` → focused `pytest` → `--once --dry-run` → restart; report only on
  action/failure/human-needed; silent when healthy).
- the env block: `OVERSEER_SOCKET`, `OVERSEER_TOKEN`, `OVERSEER_ALLOW_DESTRUCTIVE`,
  and the `PYTHONPATH=` note.
- a short table of the 9 endpoints with which are gated by the safe-mode flag.

## Error Handling

- `overseer_health.py` never raises to the caller: any read failure (missing log,
  unreadable DB, missing beacon) degrades to a `null`/`unknown` field and a logged
  note, never a crash. A *failure to determine liveness* is treated conservatively as
  unhealthy (nonzero exit) so the host wakes Hermes rather than silently assuming OK.
- The DB read opens its own short-lived `IncidentTracker` connection (read-only use);
  a locked/missing DB → `flagged: {"count": 0, "bots": [], "error": "..."}` and does
  **not** by itself flip the exit code (absence of evidence ≠ a flagged incident).
- The safe-mode refusals are ordinary `ValueError`s → the existing `_dispatch` error
  envelope; no partial action (refuse **before** any press).

## Testing

New `tests/test_overseer_health.py` (sync; real `IncidentTracker` on a tmp DB per the
fake-objects rule; temp log files; monkeypatch the process-scan):
1. all-healthy → `healthy: true`, exit 0.
2. process dead → `process_alive: false`, exit 1.
3. process alive but beacon stale (>5× interval) → `wedged: true`, exit 1.
4. beacon fresh → not wedged.
5. flagged incident present (real tracker `open(..., novel=True)`) → exit 1, bot listed.
6. recent_errors parsed from a temp err log (newest-first, bounded) — report-only,
   does **not** flip a healthy exit.
7. socket_present true/false from a temp path — report-only.
8. last_sweep parsed from a temp gui log; absent → null.
9. unreadable DB → `flagged.error` set, exit not flipped by that alone.
10. JSON is valid and `healthy` mirrors the exit code.

Append to `tests/test_tg_actions.py`:
11. real `press_button` with `allow_destructive=False` + a destructive matched label →
    `{"refused": "destructive", ...}`, no click; with `allow_destructive=True` (default)
    + `confirmed=True` it clicks (existing behavior preserved).

Append to `tests/test_overseer_api.py`:
12. `_h_press_button` forwards `allow_destructive` = `cfg.overseer_allow_destructive`
    into `press_button` (fake records the kwarg it received; off by default).
13. `_h_run_ladder` refused when flag off (does NOT reach `novel_recovery.attempt`);
    runs when `overseer_allow_destructive=True` (patch `attempt`). Update the existing
    `test_run_ladder_honors_attempt_gates` cfg to set the flag true.
14. read/diagnose endpoints (`list_flagged`, `read_bot`, `get_stats`) unaffected by
    the flag. Update the existing `press_button` monkeypatch fakes to accept
    `allow_destructive=True` in their signatures.

Append to `tests/test_config.py`: `overseer_allow_destructive` defaults False; parses
`OVERSEER_ALLOW_DESTRUCTIVE=true`.

Add a regression test (in `tests/test_overseer_health.py` or another suitable file)
asserting that after `tests/conftest.py` runs, `sys.path[0]` is the repo root — so an
external `PYTHONPATH` entry cannot shadow the repo's `tools`/`watcherdog`.

Plist and runbook are non-code (a smoke check that the plist is valid XML via
`plutil -lint` can be a manual step in the plan, not a unit test).

## Files Touched

| File | Change |
|---|---|
| `scripts/overseer_health.py` | **NEW** — standalone health/trigger JSON + exit code |
| `com.watcherdog.telegram.plist` | rewrite stale paths → this repo/venv, KeepAlive, telegram.out/err.log |
| `tests/conftest.py` | force repo root to `sys.path[0]` unconditionally |
| `tools/__init__.py` | **NEW** — explicit package marker |
| `watcherdog/config.py` | `overseer_allow_destructive` (env `OVERSEER_ALLOW_DESTRUCTIVE`, default False) |
| `watcherdog/tg_actions.py` | `press_button(..., allow_destructive=True)` — matched-label gate |
| `watcherdog/overseer_api.py` | `_h_press_button` forwards the flag; `_h_run_ladder` handler-gate |
| `tests/test_tg_actions.py` | press_button refuses destructive matched-label when `allow_destructive=False` |
| `docs/wiki/reference/Hermes Overseer Runbook.md` | **NEW** — host wiring + prompt + env + endpoint table |
| `tests/test_overseer_health.py` | **NEW** — health matrix |
| `tests/test_overseer_api.py` / `tests/test_config.py` | safe-mode + config tests |
| `README.md` | one line: the new `OVERSEER_ALLOW_DESTRUCTIVE` env + health script pointer |

## Rollout

Branch → PR → reviewer pass → merge (repo flow). No watcher restart is *required* by
this change (the health script + plist + docs don't alter the running process), but
after merge the owner can install the fixed plist (`launchctl`) and the safe-mode flag
takes effect on the next restart. The safe-mode default is **fail-safe** (destructive
refused) so a Hermes loop wired before the owner sets the flag can observe + teach but
cannot press destructive buttons.

## Open Risks

- **Process-scan portability.** Matching `run_watcher.py` in the process list must
  exclude the health script itself and any editor/grep. Mitigation: match the python
  interpreter + `run_watcher.py` argv, exclude own PID; test with a fake scanner.
- **Log-tail cost.** `gui_run.log` is large (~1 M+ lines). The script must read only
  the tail (seek from end / bounded `deque`), never load the whole file. Spec'd as a
  bounded read.
- **Safe-mode false sense of security.** The flag stops *socket-driven* destructive
  presses only; the in-process core still auto-recovers per `PANEL_AUTO_DESTRUCTIVE`.
  The runbook states this explicitly so the owner isn't surprised that panels still
  get auto-rebooted while `OVERSEER_ALLOW_DESTRUCTIVE` is false.
