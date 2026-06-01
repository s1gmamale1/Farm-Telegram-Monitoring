"""Log directory monitor.

Tails every `*.log` file in the configured directory, remembers how far it has
read (persisted to disk so restarts don't replay old errors), and groups lines
into discrete "incidents":

  * a Python traceback ("Traceback (most recent call last):" ... exception line)
  * any single line at ERROR / CRITICAL level (or containing "Exception")

Pure stdlib. Polling-based (no inotify/watchdog dependency).
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import time

logger = logging.getLogger("watcherdog.monitor")

_TRACEBACK_START = "Traceback (most recent call last):"
# A standalone error line we should treat as an incident on its own.
_ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b|Exception|Unhandled error")
# Used to normalize an error so repeated occurrences hash identically:
# strip timestamps, line numbers, hex addresses, and bare integers.
_NORMALIZE_RES = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<TS>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"line \d+"), "line <N>"),
    (re.compile(r"\b\d+\b"), "<N>"),
]


def normalize_error(text):
    """Collapse volatile parts of an error so the same bug hashes consistently."""
    out = text
    for rx, repl in _NORMALIZE_RES:
        out = rx.sub(repl, out)
    return out


def error_hash(text):
    return hashlib.sha256(normalize_error(text).encode("utf-8")).hexdigest()


class _FileState:
    __slots__ = ("offset", "inode", "tb_lines", "in_traceback", "last_activity")

    def __init__(self, offset=0, inode=None):
        self.offset = offset
        self.inode = inode
        self.tb_lines = []          # buffer for an in-progress traceback
        self.in_traceback = False
        self.last_activity = 0.0    # monotonic time of last appended tb line


class LogMonitor:
    def __init__(self, log_dir, offsets_path, *, log_glob="*.log", flush_idle_seconds=5.0):
        self.log_dir = log_dir
        self.offsets_path = offsets_path
        self.log_glob = log_glob
        self.flush_idle_seconds = flush_idle_seconds
        self.states = {}  # path -> _FileState
        os.makedirs(self.log_dir, exist_ok=True)
        self._load_offsets()

    # --- offset persistence -------------------------------------------------
    def _load_offsets(self):
        if not os.path.exists(self.offsets_path):
            return
        try:
            with open(self.offsets_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for path, info in saved.items():
                self.states[path] = _FileState(
                    offset=info.get("offset", 0), inode=info.get("inode")
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load offsets file: %s", exc)

    def _save_offsets(self):
        try:
            os.makedirs(os.path.dirname(self.offsets_path), exist_ok=True)
            tmp = self.offsets_path + ".tmp"
            data = {
                p: {"offset": s.offset, "inode": s.inode}
                for p, s in self.states.items()
            }
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.offsets_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save offsets file: %s", exc)

    # --- core polling -------------------------------------------------------
    def poll(self):
        """Read new content from all log files and return a list of incidents.

        Each incident is a dict: {"bot": str, "text": str}.
        """
        incidents = []
        now = time.monotonic()

        for path in sorted(glob.glob(os.path.join(self.log_dir, self.log_glob))):
            try:
                st = os.stat(path)
            except OSError:
                continue

            state = self.states.get(path)
            if state is None:
                # New file discovered at runtime: read from the START. logs/ is
                # WatcherDog's dedicated directory that bots write into, so there
                # is no large pre-existing backlog to fear — and the dedupe window
                # plus persisted offsets prevent re-alerting on the same error.
                state = _FileState(offset=0, inode=st.st_ino)
                self.states[path] = state

            # Detect rotation/truncation: inode changed or file shrank.
            if state.inode != st.st_ino or st.st_size < state.offset:
                state.offset = 0
                state.inode = st.st_ino
                state.tb_lines = []
                state.in_traceback = False

            if st.st_size > state.offset:
                new_text = self._read_from(path, state.offset)
                state.offset += len(new_text.encode("utf-8", errors="replace"))
                bot_name = self._bot_name(path)
                self._feed_lines(state, new_text, bot_name, incidents, now)

            # Flush a traceback that has gone quiet (split across reads / last in file).
            if (
                state.in_traceback
                and state.tb_lines
                and (now - state.last_activity) >= self.flush_idle_seconds
            ):
                incidents.append(
                    {"bot": self._bot_name(path), "text": "".join(state.tb_lines)}
                )
                state.tb_lines = []
                state.in_traceback = False

        if incidents or self.states:
            self._save_offsets()
        return incidents

    def _read_from(self, path, offset):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()

    @staticmethod
    def _bot_name(path):
        return os.path.splitext(os.path.basename(path))[0]

    def _feed_lines(self, state, text, bot_name, incidents, now):
        for line in text.splitlines(keepends=True):
            if _TRACEBACK_START in line:
                # Starting a traceback: flush any prior one first.
                if state.tb_lines:
                    incidents.append({"bot": bot_name, "text": "".join(state.tb_lines)})
                state.tb_lines = [line]
                state.in_traceback = True
                state.last_activity = now
                continue

            if state.in_traceback:
                state.tb_lines.append(line)
                state.last_activity = now
                stripped = line.rstrip("\n")
                # The exception summary line ends a traceback: non-empty and
                # not indented (file frames are indented with spaces).
                if stripped and not stripped[0].isspace():
                    incidents.append({"bot": bot_name, "text": "".join(state.tb_lines)})
                    state.tb_lines = []
                    state.in_traceback = False
                continue

            # Not in a traceback: a standalone ERROR/CRITICAL line is its own incident.
            if _ERROR_LINE_RE.search(line):
                incidents.append({"bot": bot_name, "text": line.rstrip("\n")})
