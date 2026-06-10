"""Safe self-restart: apply the bot's own code changes by relaunching, with
pre-flight validation and automatic rollback.

Two layers of safety:

  1. **Pre-flight** (here): before restarting, ``validate()`` checks the WHOLE
     project still imports in a fresh subprocess. If it doesn't, the latest
     self-edit(s) are rolled back and NO restart happens — the running process is
     untouched (it still holds the old, working code in memory).
  2. **Post-flight** (restart_helper.py): a detached supervisor relaunches and
     watches for the new process to become healthy; if it doesn't, it restores
     the backups and relaunches again.

Self-edits are journalled (``cfg.self_edits_path``) as {path_abs, backup} so both
layers can undo them. ``mark_healthy()`` touches the beacon the supervisor waits
on; the journal is dropped once a restart is confirmed healthy.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger("watcherdog.restart")


# --- edit journal -----------------------------------------------------------
def _read_journal(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_journal(path, entries):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("could not write edit journal: %s", exc)


def record_edit(cfg, path_abs, backup):
    """Note a self-edit so a failed restart can undo it. `backup` "" = new file."""
    p = getattr(cfg, "self_edits_path", None)
    if not p:
        return
    entries = _read_journal(p)
    entries.append({"path_abs": path_abs, "backup": backup or "", "ts": int(time.time())})
    _write_journal(p, entries)


def _rollback_latest(cfg):
    """Undo the newest journalled edit (restore its backup, or delete a new file).
    Returns the restored path, or None if the journal is empty."""
    p = getattr(cfg, "self_edits_path", None)
    entries = _read_journal(p) if p else []
    if not entries:
        return None
    e = entries.pop()
    _write_journal(p, entries)
    path, bak = e.get("path_abs"), e.get("backup")
    try:
        if bak and os.path.exists(bak):
            with open(bak, "rb") as s, open(path, "wb") as d:
                d.write(s.read())
        elif not bak and path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log.warning("rollback of %s failed: %s", path, exc)
    return path


# --- health beacon ----------------------------------------------------------
def mark_healthy(cfg):
    """Touch the health beacon — called once the watcher is fully up. The restart
    supervisor watches this file's mtime to confirm a relaunch succeeded."""
    p = getattr(cfg, "watcher_health_path", None)
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {int(time.time())}\n")
    except OSError as exc:
        log.debug("could not write health beacon: %s", exc)


# --- validation + restart ---------------------------------------------------
def validate(root, python, timeout=90):
    """Import the whole project in a fresh subprocess. Returns (ok, detail)."""
    try:
        proc = subprocess.run(
            [python, "-c", "import run_watcher"], cwd=root,
            capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    out = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, out[-1500:]


def _spec(cfg, python, root):
    data_dir = os.path.dirname(cfg.self_edits_path) or "."
    return {
        "pid": os.getpid(), "python": python, "root": root,
        "argv": list(sys.argv),
        "logfile": os.path.join(data_dir, "telegram.out.log"),
        "health_path": cfg.watcher_health_path,
        "edits_path": cfg.self_edits_path,
        "delay": 6, "health_timeout": 90,
    }


def _launch_helper(spec):
    data_dir = os.path.dirname(spec["edits_path"]) or "."
    spec_path = os.path.join(data_dir, "restart_spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)
    subprocess.Popen(
        [spec["python"], "-m", "watcherdog.restart_helper", spec_path],
        cwd=spec["root"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def request_restart(cfg):
    """Validate the current on-disk code and, if good, hand off to the detached
    supervisor to relaunch. If it doesn't import, roll back the offending self-
    edit(s) and do NOT restart. Returns a JSON-able result dict."""
    if not getattr(cfg, "bot_self_restart_enabled", False):
        return {"error": "self-restart is disabled (BOT_SELF_RESTART_ENABLED=false)"}
    root, python = cfg.root, sys.executable
    ok, detail = validate(root, python)
    rolled = []
    while not ok:
        path = _rollback_latest(cfg)
        if path is None:
            return {"error": "won't restart — the code does not import and there is "
                             f"nothing left to roll back:\n{detail}"}
        rolled.append(os.path.relpath(path, root))
        ok, detail = validate(root, python)
    if rolled:
        return {"restarted": False, "rolled_back": rolled,
                "note": "That change broke the build, so I rolled it back. Nothing "
                        "was restarted — the bot keeps running the previous code."}
    try:
        _launch_helper(_spec(cfg, python, root))
    except Exception as exc:  # noqa: BLE001
        log.exception("could not launch restart helper")
        return {"error": f"could not start the restart: {exc}"}
    log.info("self-restart requested — validated; supervisor launched.")
    return {"restarting": True,
            "note": "Validated ✅ — restarting now. I'll be back online in a few seconds."}
