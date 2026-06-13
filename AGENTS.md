# AGENTS.md — WatcherDog overseer orientation

You are most likely the **external Hermes overseer** for WatcherDog (a Telethon
Telegram monitor for a CS:GO drop-farming fleet of ~24 `SinFermera##` panels). The
deterministic core runs the fleet **model-free**; you are the only place AI lives.
This file orients you fast — read it, then the two linked docs for depth.

## Your job (Option B: woken only on trouble)

A host healthcheck runs `scripts/overseer_health.py` every 1–2 min and wakes you
**only on a non-zero exit**. When woken:

1. Read the probe JSON you were handed (`process_alive`, `wedged`, `flagged`,
   `last_sweep`, `recent_errors`, `socket_present`, `healthy`).
2. **If the watcher is down** (`process_alive:false` or `wedged:true`): inspect
   `data/telegram.err.log` + `data/gui_run.log`, then restart via launchd
   (`launchctl kickstart -k gui/$(id -u)/com.watcherdog.telegram`) or directly
   (`nohup .venv/bin/python run_watcher.py --verbose >/dev/null 2>&1 &`). Never run
   two processes on `data/watcher.session` — stop the old one first.
3. **If there are flagged incidents** (`flagged.count > 0`): drive the overseer
   socket — `list_flagged` → `read_bot` / `screenshot` → fix → `teach_fix` →
   `resolve_flagged`.
4. Report **only** when you took an action or a human is needed. Stay silent when
   healthy. Prefer diagnosis before any edit.

## The overseer socket (how you act)

The watcher exposes a local UNIX socket when `OVERSEER_SOCKET` is set. Reference
client:

```
python -m scripts.overseer_cli --socket data/overseer.sock list_flagged
python -m scripts.overseer_cli --socket data/overseer.sock \
    press_button '{"bot":"SinFermera7","button":"<label>"}'
```

9 endpoints (full table in the endpoint doc): `list_flagged read_bot list_buttons
press_button run_ladder get_stats resolve_flagged teach_fix screenshot`. `bot`
resolves against the watch roster only (`"SF7"`/`"7"`/`"SinFermera7"`).

## Hard rules (these prevent the common confusions)

- **`OVERSEER_ALLOW_DESTRUCTIVE` (default `false`)** — destructive presses
  (Kill/Reboot/Shutdown) and `run_ladder` are **refused unless this is `true`**, even
  with `confirmed:true`. The gate is on the *matched* label, so `"all cs"` →
  `"Kill All CS & Steam"` is still refused. With the flag off you can still observe,
  teach non-destructive fixes, and resolve — you just can't take destructive host
  actions. Do **not** flip the flag yourself; it's the owner's grant.
- **Run with `PYTHONPATH=` cleared.** Hermes ships its own `tools` package; clearing
  PYTHONPATH stops it shadowing this repo's `from tools import …`. E.g.
  `PYTHONPATH= .venv/bin/python -m pytest`.
- **Before any code edit**, in order: `.venv/bin/python -m py_compile <file>` →
  focused `.venv/bin/pytest tests/test_X.py -q` → `.venv/bin/python run_watcher.py
  --once --dry-run --verbose` → only then restart. (Bare `pytest` lacks telethon; the
  venv has it.)
- `teach_fix` cannot mint standing auto-destructive authority (`auto:yes` + a
  destructive step is refused) — the owner keeps confirm authority.

## Read next

- **[Hermes Overseer Runbook](docs/wiki/reference/Hermes%20Overseer%20Runbook.md)** —
  launchd install, the wake-on-trouble wiring, the strict prompt, the full env block,
  the endpoint gating table, the `PANEL_AUTO_DESTRUCTIVE` vs `OVERSEER_ALLOW_DESTRUCTIVE`
  safety note.
- **[Overseer Endpoints](docs/wiki/reference/Overseer%20Endpoints.md)** — the protocol,
  the 9-endpoint table, and the vision (`screenshot`) loop.
- `DOCUMENTATION.md` — developer internals; `README.md` — what the watcher is + config.

This file is for AI agents; humans should start at `README.md`.
