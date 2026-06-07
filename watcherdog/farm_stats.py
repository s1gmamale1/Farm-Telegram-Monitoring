"""Deterministic parser for the FSM panel control-bot status message.

Pure functions — no model, no Telegram. Turns the auto-updating
"FSM Panel - Main menu" message into a typed PanelStatus. Every field is
optional: anything we can't parse stays None (we never guess a number).
Reused by panel_rules and (later) the report commands. See
docs/wiki/reference/Panel Control Bot.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time as dtime

_LAUNCHED_RE = re.compile(r"Launched:\s*(\d+)\s*account", re.I)
_STATUS_RE = re.compile(r"Status:[ \t]*([^\n│|]+)", re.I)
_MAP_RE = re.compile(r"Map:\s*([^\n│|]+)", re.I)
_SCORE_RE = re.compile(r"Score:\s*(\[[^\]]*\]|\S+)", re.I)
_TOTAL_RE = re.compile(r"Total:\s*(\d+)", re.I)
_UPDATED_RE = re.compile(r"Updated:\s*(\d{1,2}):(\d{2}):(\d{2})", re.I)
_ALERT_RE = re.compile(r"All\s+(\d+)\s+accounts?\s+launched", re.I)
_ACC_RE = re.compile(r"^[│├└|\s]*(\d+)\.\s*([^\s│|]+)", re.M)


@dataclass
class Account:
    slot: int | None = None
    name: str | None = None


@dataclass
class PanelStatus:
    launched: int | None = None
    status: str | None = None
    map: str | None = None
    score: str | None = None
    in_match: bool = False
    accounts: list = field(default_factory=list)
    total: int | None = None
    updated_at: dtime | None = None
    raw: str = ""


def _clean(s):
    return s.strip().strip("│|").strip() if s else None


def _int(rx, text):
    m = rx.search(text)
    return int(m.group(1)) if m else None


def parse_panel_status(text):
    """Parse a panel status message. Never raises; unknown fields stay None."""
    text = text or ""
    st = PanelStatus(raw=text)
    st.launched = _int(_LAUNCHED_RE, text)
    st.total = _int(_TOTAL_RE, text)
    m = _STATUS_RE.search(text)
    if m:
        st.status = _clean(m.group(1))
    m = _MAP_RE.search(text)
    if m:
        st.map = _clean(m.group(1))
    m = _SCORE_RE.search(text)
    if m:
        st.score = _clean(m.group(1))
    m = _UPDATED_RE.search(text)
    if m:
        try:
            st.updated_at = dtime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            st.updated_at = None
    st.in_match = bool(st.map and st.score)
    for am in _ACC_RE.finditer(text):
        st.accounts.append(Account(slot=int(am.group(1)), name=am.group(2)))
    return st


def launched_from_alert(text):
    """Pull N from 'All N accounts launched!' style alerts, else None."""
    m = _ALERT_RE.search(text or "")
    return int(m.group(1)) if m else None
