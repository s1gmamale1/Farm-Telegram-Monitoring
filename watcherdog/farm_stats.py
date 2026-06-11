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

_DROP_HEADER_RE = re.compile(r"DROP REPORT", re.I)
_PRICE_RE = re.compile(r"Price of all drop:\s*~?\s*([\d.]+)\s*\$", re.I)
_TOTALCASES_RE = re.compile(r"Total cases:\s*(\d+)\s*pcs", re.I)
_REPORT_ACCTS_RE = re.compile(r"^Accounts:\s*(\d+)\s*$", re.I | re.M)
_CANTGET_RE = re.compile(r"Can'?t get drop on\s+(\d+)\s+accounts?", re.I)
# a table row "Name | int | num" (cases: 2 cols after name; skins: name | amount | price)
_ROW_RE = re.compile(r"^(.*?\S)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*$", re.M)


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
    text = text if isinstance(text, str) else ""
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


@dataclass
class DropItem:
    name: str | None = None
    amount: int | None = None
    price: float | None = None        # only set for skins


@dataclass
class DropReport:
    value_usd: float | None = None    # the panel's own "Price of all drop: ~X$"
    total_cases: int | None = None    # "Total cases: N pcs"
    accounts: int | None = None       # "Accounts: N" in the report
    cases: list = field(default_factory=list)    # DropItem(name, amount)
    skins: list = field(default_factory=list)    # DropItem(name, amount, price)
    problems: list = field(default_factory=list)  # e.g. "Can't get drop on N accounts"
    raw: str = ""


def _table_rows(section):
    """Yield (name, amount, third) for 'Name | int | num' rows in a section."""
    for m in _ROW_RE.finditer(section):
        yield _clean(m.group(1)), int(m.group(2)), float(m.group(3))


def parse_drop_report(text):
    """Parse a panel 'Drop Stats' reply. Never raises; no DROP REPORT header and no
    'Can't get drop' line -> an all-None report (echo/empty press artifact)."""
    text = text if isinstance(text, str) else ""
    r = DropReport(raw=text)
    cant = _CANTGET_RE.search(text)
    if cant:
        r.problems.append(f"Can't get drop on {cant.group(1)} accounts")
    if not _DROP_HEADER_RE.search(text):
        return r                       # echo/empty -> only the problem (if any) is set
    m = _PRICE_RE.search(text)
    r.value_usd = float(m.group(1)) if m else None
    m = _TOTALCASES_RE.search(text)
    r.total_cases = int(m.group(1)) if m else None
    m = _REPORT_ACCTS_RE.search(text)
    r.accounts = int(m.group(1)) if m else None
    # Split into the Case section and the Skin section by their headers.
    case_hdr = re.search(r"Case\s*\|\s*Amount\s*\|\s*% of drops", text, re.I)
    skin_hdr = re.search(r"Skin[^\n|]*\|\s*Amount\s*\|\s*Price", text, re.I)
    case_start = case_hdr.end() if case_hdr else None
    skin_start = skin_hdr.end() if skin_hdr else None
    if case_start is not None:
        case_section = text[case_start:(skin_hdr.start() if skin_hdr else len(text))]
        for name, amt, _pct in _table_rows(case_section):
            r.cases.append(DropItem(name=name, amount=amt))
    if skin_start is not None:
        skin_section = text[skin_start:]
        # stop before the summary (➙ lines)
        cut = skin_section.find("➙")
        if cut != -1:
            skin_section = skin_section[:cut]
        for name, amt, price in _table_rows(skin_section):
            r.skins.append(DropItem(name=name, amount=amt, price=price))
    return r


_EVENT_PATTERNS = [
    ("launch_error",    re.compile(r"error while launching", re.I)),
    ("crash_recovered", re.compile(r"crashed and restarted", re.I)),
    ("match_cancelled", re.compile(r"match cancelled", re.I)),
    ("match_ended",     re.compile(r"match ended", re.I)),
    ("lobby_creating",  re.compile(r"starting lobby creation", re.I)),
    ("warmup",          re.compile(r"warmup started", re.I)),
    ("launched",        re.compile(r"all\s+\d+\s+accounts?\s+launched", re.I)),
]


def parse_status_event(text):
    """Map a panel's latest message to a canonical event name, or None if novel.
    Order matters: error/crash signals win over the routine activity lines. The
    bot-tag ([...], incl. the SinFarmera typo / odd casing) is ignored — matching
    is on the message body substring. No model."""
    text = text if isinstance(text, str) else ""
    for name, rx in _EVENT_PATTERNS:
        if rx.search(text):
            return name
    return None
