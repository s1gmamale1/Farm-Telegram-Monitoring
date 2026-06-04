"""Persistent record of the tasks the bot is actively working on.

When the bot starts an action task ("close all accounts", "stop the panels"),
it records it here. If the watcher is killed/restarted mid-task, the record
survives, and on startup the bot re-runs the task ("resuming after a restart").
Finished tasks are removed. A resume counter caps how many times a task may be
retried so a task that crashes the process can't loop forever.

Pure stdlib; atomic writes (temp + os.replace) guarded by a lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("watcherdog.tasks")

_LOCK = threading.Lock()
_MAX_PROGRESS = 20   # keep only the most recent progress lines


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"seq": 0, "tasks": []}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return {"seq": 0, "tasks": []}
    data.setdefault("seq", 0)
    return data


def _write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def start(path, chat_id, user_id, request):
    """Record a new in-progress task. Returns its id."""
    with _LOCK:
        data = _read(path)
        tid = int(data.get("seq", 0)) + 1
        data["seq"] = tid
        data["tasks"].append({
            "id": tid, "chat_id": chat_id, "user_id": user_id,
            "request": request, "status": "in_progress", "progress": [],
            "resume_count": 0, "started_at": int(time.time()),
        })
        _write(path, data)
    return tid


def update(path, task_id, label):
    """Append a progress label to a task (most recent ones kept)."""
    with _LOCK:
        data = _read(path)
        for t in data["tasks"]:
            if t.get("id") == task_id:
                t.setdefault("progress", []).append(label)
                t["progress"] = t["progress"][-_MAX_PROGRESS:]
                _write(path, data)
                return


def finish(path, task_id):
    """Remove a task (it's done or abandoned)."""
    with _LOCK:
        data = _read(path)
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        if len(data["tasks"]) != before:
            _write(path, data)


def bump_resume(path, task_id):
    """Increment a task's resume counter; return the new count."""
    with _LOCK:
        data = _read(path)
        for t in data["tasks"]:
            if t.get("id") == task_id:
                t["resume_count"] = int(t.get("resume_count", 0)) + 1
                _write(path, data)
                return t["resume_count"]
    return 0


def active(path):
    """Return the in-progress tasks (a copy)."""
    return [dict(t) for t in _read(path).get("tasks", [])
            if t.get("status") == "in_progress"]
