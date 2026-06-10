"""Detached supervisor that restarts run_watcher SAFELY after a self-edit.

Invoked as a detached subprocess::

    python -m watcherdog.restart_helper <spec.json>

It deliberately imports NOTHING from ``watcherdog`` (pure stdlib) so it keeps
working even if the self-edit broke the package. The flow:

  1. wait a moment (so the bot can deliver its "restarting…" reply),
  2. stop the old watcher (SIGTERM, then SIGKILL),
  3. start the new one and wait for it to touch its health beacon,
  4. if it never becomes healthy (crash / won't boot), restore the self-edit
     backups and start it again — so a bad edit can never leave the bot down.

The journal (``edits_path``) lists {path_abs, backup} for each pending edit;
backup "" means the file was newly created, so rollback deletes it.
"""

import json
import os
import signal
import subprocess
import sys
import time


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop(pid, grace=10):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(grace):
        if not _alive(pid):
            return
        time.sleep(1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _start(python, argv, root, logfile):
    out = open(logfile, "ab")
    proc = subprocess.Popen([python] + list(argv), cwd=root, stdout=out, stderr=out,
                            start_new_session=True)
    return proc.pid


def _wait_healthy(health_path, since, new_pid, timeout):
    """True once the new watcher touches its health beacon after `since`; False if
    it dies first or the timeout elapses."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if os.path.getmtime(health_path) >= since:
                return True
        except OSError:
            pass
        if not _alive(new_pid):
            return False
        time.sleep(1)
    try:
        return os.path.getmtime(health_path) >= since
    except OSError:
        return False


def _rollback(edits_path):
    try:
        with open(edits_path, "r", encoding="utf-8") as fh:
            edits = json.load(fh)
    except Exception:
        return []
    restored = []
    for e in reversed(edits):  # newest first
        path = e.get("path_abs")
        bak = e.get("backup")
        if not path:
            continue
        try:
            if bak and os.path.exists(bak):
                with open(bak, "rb") as s, open(path, "wb") as d:
                    d.write(s.read())
                restored.append(path)
            elif not bak and os.path.exists(path):
                os.remove(path)            # it was a newly-created file
                restored.append(path)
        except OSError:
            pass
    return restored


def _drop(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _acquire_singleton_lock(path, ttl=300):
    """True if we got the lock; False if another live supervisor holds it.

    Two near-simultaneous restart requests would otherwise spawn two supervisors,
    each starting its own watcher against the SAME ``watcher.session`` — which
    corrupts the Telegram auth key (the known failure class this guards against).

    The lock is an atomic ``O_CREAT | O_EXCL`` create, so out of N racing helpers
    exactly one wins. A lock older than ``ttl`` seconds is assumed to belong to a
    crashed prior helper and is reclaimed (one recursive retry; the retry either
    creates a fresh lock or loses the race to another reclaimer → returns False,
    so it can never spin).
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > ttl:
                os.remove(path)            # stale: a crashed prior supervisor
                return _acquire_singleton_lock(path, ttl)
        except OSError:
            pass
        return False


def main():
    if len(sys.argv) < 2:
        return
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    lock_path = sys.argv[1] + ".lock"
    if not _acquire_singleton_lock(lock_path):
        return  # another supervisor is already restarting — don't double-start the session
    try:
        time.sleep(spec.get("delay", 6))
        _stop(spec["pid"])

        since = time.time()
        try:
            new_pid = _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])
        except Exception:  # noqa: BLE001 — old process is already dead; one retry relaunches it
            new_pid = _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])

        if _wait_healthy(spec["health_path"], since, new_pid, spec.get("health_timeout", 90)):
            _drop(spec["edits_path"])      # the edits are good — commit them
            _drop(sys.argv[1])
            return

        # The new code didn't come up healthy: stop it, roll the edits back, retry.
        _stop(new_pid)
        _rollback(spec["edits_path"])
        _drop(spec["edits_path"])
        _start(spec["python"], spec["argv"], spec["root"], spec["logfile"])
        _drop(sys.argv[1])
    finally:
        _drop(lock_path)


if __name__ == "__main__":
    main()
