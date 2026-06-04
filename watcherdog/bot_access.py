"""Persistent, runtime-editable bot access list.

The conversation agent can GRANT or REVOKE "use the bot" (panel-action) access
to a user on an admin's command. Those grants are stored here as JSON so they
survive restarts and take effect without editing `.env` or touching code. The
bot unions these granted ids with the static BOT_ACTION_USERS each turn.

Pure stdlib; writes are atomic (temp file + os.replace) and guarded by a lock so
concurrent agent turns can't corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger("watcherdog.access")

_LOCK = threading.Lock()


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"users": []}
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return {"users": []}
    return data


def _write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def list_users(path):
    """Return the granted users as [{"id", "label"}], newest last."""
    return list(_read(path).get("users", []))


def granted_ids(path):
    """The set of granted user ids (ints). Empty set if none / unreadable."""
    out = set()
    for u in _read(path).get("users", []):
        sid = str(u.get("id", "")).strip()
        if sid.lstrip("-").isdigit():
            out.add(int(sid))
    return out


def grant(path, user_id, label=""):
    """Grant `user_id` bot-action access. Returns True if newly added, False if
    they already had access (the label is refreshed either way)."""
    uid = int(user_id)
    with _LOCK:
        data = _read(path)
        users = data.setdefault("users", [])
        for u in users:
            if str(u.get("id")) == str(uid):
                if label:
                    u["label"] = label
                _write(path, data)
                return False
        users.append({"id": uid, "label": label or str(uid)})
        _write(path, data)
    log.info("granted bot access to %s (%s)", uid, label or "")
    return True


def revoke(path, user_id):
    """Revoke `user_id`'s access. Returns True if they had it, False otherwise."""
    uid = int(user_id)
    with _LOCK:
        data = _read(path)
        users = data.get("users", [])
        kept = [u for u in users if str(u.get("id")) != str(uid)]
        data["users"] = kept
        _write(path, data)
    removed = len(kept) != len(users)
    if removed:
        log.info("revoked bot access from %s", uid)
    return removed
