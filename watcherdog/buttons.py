"""Inline confirm/action buttons — tappable by anyone in the group (Phase 3.5).

No more typed yes/no. Whenever WatcherDog needs a confirmation or wants to offer
quick one-tap panel actions, it posts a message with inline buttons under it. A
tap arrives as a Bot-API callback the bot handles **deterministically** (no LLM).

Each card's buttons are backed by an in-process registry keyed by an unguessable
id and signed with a per-run secret, so a button only ever runs the exact action
it was posted for, **once**, and only before it expires. The press is executed on
the USER account (a bot can't press a farm panel's buttons); the card is then
edited to show the result and who tapped it. Per the owner's choice, **any** group
member may tap — the signed single-use token is the authorization, not the user.

This module is Telethon-free (pure stdlib) so the token/registry logic is unit-
testable. ``BotInterface`` wraps the ``(label, data)`` rows it returns into
Telethon inline buttons.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger("watcherdog.buttons")

# Callback-data prefix. Telegram caps callback data at 64 bytes; our format
# "wd:<12-hex id>:<idx>:<10-hex sig>" is ~32 bytes, well within budget.
_PREFIX = "wd"
_SIG_LEN = 10


class ActionRegistry:
    """Tracks the pending action cards this process has posted.

    An *option* is a dict ``{"key", "label", "steps"}`` where ``steps`` is the
    ordered list of panel-button labels to press (empty = a no-op like "Skip").
    """

    def __init__(self, secret=None, ttl=900.0):
        if secret is None:
            secret = os.urandom(16).hex()
        self._secret = secret.encode() if isinstance(secret, str) else bytes(secret)
        self._ttl = float(ttl)
        self._pending = {}  # action_id -> {target, options, created, used, title}

    # --- token signing ------------------------------------------------------
    def sign(self, action_id, idx):
        """Short HMAC of (action_id, idx) — proves WE minted this button."""
        mac = hmac.new(self._secret, f"{action_id}:{idx}".encode(), hashlib.sha256)
        return mac.hexdigest()[:_SIG_LEN]

    def _data(self, action_id, idx):
        return f"{_PREFIX}:{action_id}:{idx}:{self.sign(action_id, idx)}"

    def parse(self, data):
        """Validate callback data and return ``(action_id, idx)`` or ``None``.

        Rejects anything malformed or whose signature doesn't verify, so forged
        or tampered callback data never resolves to a real action.
        """
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "ignore")
        parts = (data or "").split(":")
        if len(parts) != 4:
            return None
        prefix, action_id, idx, sig = parts
        if prefix != _PREFIX or not action_id:
            return None
        try:
            idx = int(idx)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self.sign(action_id, idx)):
            return None
        return action_id, idx

    # --- lifecycle ----------------------------------------------------------
    def add(self, target, options, *, title="", now=None):
        """Register a card's options against a panel ``target``.

        Returns ``(action_id, rows)`` where ``rows`` is a list of
        ``(label, callback_data)`` for the buttons to render.
        """
        self.purge(now=now)
        action_id = os.urandom(6).hex()
        self._pending[action_id] = {
            "target": target, "options": list(options),
            "created": now if now is not None else time.time(),
            "used": False, "title": title,
        }
        rows = [(opt["label"], self._data(action_id, i))
                for i, opt in enumerate(options)]
        return action_id, rows

    def resolve(self, data, *, now=None):
        """Resolve a tapped button to an outcome the handler acts on.

        Returns ``(status, entry, option)`` where status is one of:
        ``"ok"`` (run it), ``"expired"``, ``"used"`` (already tapped), or
        ``"invalid"`` (bad signature / unknown / gone).
        """
        parsed = self.parse(data)
        if parsed is None:
            return ("invalid", None, None)
        action_id, idx = parsed
        entry = self._pending.get(action_id)
        if entry is None:
            return ("invalid", None, None)
        now = now if now is not None else time.time()
        if now - entry["created"] > self._ttl:
            return ("expired", entry, None)
        if entry["used"]:
            return ("used", entry, None)
        if idx < 0 or idx >= len(entry["options"]):
            return ("invalid", entry, None)
        return ("ok", entry, entry["options"][idx])

    def consume(self, data):
        """Mark the whole card used (single-use) so no button replays."""
        parsed = self.parse(data)
        if parsed is not None:
            entry = self._pending.get(parsed[0])
            if entry is not None:
                entry["used"] = True

    def purge(self, *, now=None):
        """Drop expired cards so the registry can't grow unbounded."""
        now = now if now is not None else time.time()
        dead = [aid for aid, e in self._pending.items()
                if now - e["created"] > self._ttl]
        for aid in dead:
            self._pending.pop(aid, None)


# --- option presets ---------------------------------------------------------
# The documented recovery sequence (skill 0): Kill everything, re-select the
# canonical UNFARMED batch (never "Select first 4/10 accs"), then start it.
# Matched case-insensitively (exact/prefix/substring) against the live labels.
# The select step uses the bare "unfarmed" discriminator (same as
# panel_actions.BTN_SELECT_UNFARMED) — it uniquely picks "Select N/M unfarmed"
# regardless of the account count and never the forbidden "first N/M accs".
RELAUNCH_STEPS = ["Kill All CS & Steam", "unfarmed", "Start selected accounts"]


def relaunch_options():
    """A silent/dead panel: offer relaunch, a screenshot, or skip."""
    return [
        {"key": "relaunch", "label": "🔁 Relaunch", "steps": list(RELAUNCH_STEPS)},
        {"key": "screenshot", "label": "📸 Screenshot", "steps": ["Screenshot"]},
        {"key": "skip", "label": "✋ Skip", "steps": []},
    ]


def confirm_options(steps, *, do_label="✅ Do it"):
    """A proposed (often destructive) fix: confirm to run it, or skip."""
    return [
        {"key": "do", "label": do_label, "steps": list(steps)},
        {"key": "skip", "label": "✋ Skip", "steps": []},
    ]


def is_noop(option):
    """True for a Skip/dismiss button (nothing to press)."""
    return not (option or {}).get("steps")
