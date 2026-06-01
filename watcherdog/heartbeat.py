"""Heartbeat / silence detection.

Every message from a bot is a heartbeat. If a bot that normally reports goes
quiet for longer than the threshold, it has probably crashed/stalled and we
alert once. When it speaks again, we mark it recovered (so a later silence
re-alerts, and we can announce it's back).

Bots are auto-learned the first time they post; an optional expected-bots list
lets you monitor specific bots from startup.

Restart safety: on load, every known bot's clock is reset to startup time, so a
restart never floods you with false "silent" alerts for the downtime gap.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("watcherdog.heartbeat")


class HeartbeatMonitor:
    def __init__(self, path, threshold_seconds, start_time, expected_bots=None):
        self.path = path
        self.threshold = threshold_seconds
        self.start_time = start_time
        self.last_seen = {}      # bot -> last message time
        self.alerted = set()     # bots currently flagged silent
        self._load(start_time)
        for bot in (expected_bots or []):
            self.last_seen.setdefault(bot, start_time)

    # --- persistence --------------------------------------------------------
    def _load(self, start_time):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            # Reset every known bot's clock to startup: we can't trust silence
            # that may have occurred while the watcher itself was down.
            for bot in saved.get("bots", []):
                self.last_seen[bot] = start_time
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load heartbeats: %s", exc)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"bots": sorted(self.last_seen.keys()), "last_seen": self.last_seen},
                    fh,
                )
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save heartbeats: %s", exc)

    # --- core ---------------------------------------------------------------
    def record(self, bot, now):
        """Register a heartbeat. Returns True if this bot was flagged silent and
        has just recovered."""
        recovered = bot in self.alerted
        new_bot = bot not in self.last_seen
        self.last_seen[bot] = now
        self.alerted.discard(bot)
        if recovered or new_bot:
            self._save()
        return recovered

    def check(self, now):
        """Return a list of (bot, silent_seconds) for bots that JUST crossed the
        silence threshold (not already alerted)."""
        # Grace period: don't alert until the watcher has been up at least one
        # threshold, so a fresh start doesn't misread the warm-up gap.
        if (now - self.start_time) < self.threshold:
            return []
        newly_silent = []
        for bot, ts in self.last_seen.items():
            if (now - ts) > self.threshold and bot not in self.alerted:
                self.alerted.add(bot)
                newly_silent.append((bot, now - ts))
        if newly_silent:
            self._save()
        return newly_silent
