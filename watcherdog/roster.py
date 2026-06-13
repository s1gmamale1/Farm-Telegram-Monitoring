"""Deterministic roster scan — classify every farm bot with NO LLM.

Single source of truth for "how is each bot doing", shared by the hourly report
and the fast slash-commands (``/status``, ``/problems``, ``/silent``). Reads each
bot's latest message over Telethon and buckets it with the cheap ``classifier``
plus a couple of simple heuristics (account count, farming keywords, age). No
model call — so these answers are instant and free.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from watcherdog import tg_tools
from watcherdog.classifier import classify, summarize

logger = logging.getLogger("watcherdog.roster")

# Status buckets (the emoji is part of the label, as the hourly report expects).
FARMING = "✅ farming"
QUIET = "⚠️ quiet"
ATTENTION = "🔴 needs attention"
DEAD = "💀 dead"

# Live farm-loop vocabulary — a panel emitting any of these is actively working
# (warming up, matchmaking, building a lobby, launching accounts), so a FRESH
# message containing one reads as ✅ farming. Kept broad on the farming side
# because it only ever promotes a fresh panel QUIET→FARMING; it never overrides
# the error/accounts/stale/dead flags, which are decided first.
_FARMING_KEYWORDS = re.compile(r"\b(warm\s*up|match|lobby|launch(?:ing|ed)?)\b", re.I)
_ACCOUNTS_RE = re.compile(r"accounts?\s*[=:]\s*(\d+)", re.I)
_BOT_NUM_RE = re.compile(r"(\d+)")

_pc_map_cache = None


def status_emoji(status):
    """Just the leading emoji for a status (for compact PC rows)."""
    return {FARMING: "✅", QUIET: "⚠️", ATTENTION: "🔴", DEAD: "💀"}.get(status, "❓")


def extract_account_count(text):
    """The 'accounts: N' figure a panel reports, or None."""
    m = _ACCOUNTS_RE.search(text or "")
    return int(m.group(1)) if m else None


def farming_indicator(text):
    """True if the message looks like live farming (warmup / match / lobby)."""
    return bool(_FARMING_KEYWORDS.search(text or ""))


def _invert_pc_map(raw):
    """Build a bot_number -> PC map. Accepts either {PC: [bot, bot]} (the natural
    way to write it — inverted here) or an already bot-keyed {bot: PC}."""
    mapping = {}
    for k, v in raw.items():
        if isinstance(v, (list, tuple)):
            for b in v:
                mapping[int(b)] = str(k)
        else:
            mapping[int(k)] = str(v)
    return mapping


def load_pc_map(cfg):
    """bot_number -> PC name, from data/farmer_pc_map.json (cached). {} on error."""
    global _pc_map_cache
    if _pc_map_cache is not None:
        return _pc_map_cache
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "data", "farmer_pc_map.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _pc_map_cache = _invert_pc_map(raw)
        logger.info("loaded pc map: %d bots across %d PCs", len(_pc_map_cache),
                    len(set(_pc_map_cache.values())))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not load farmer_pc_map.json (%s); using empty map", exc)
        _pc_map_cache = {}
    return _pc_map_cache


def classify_status_detailed(text, age_min, cfg):
    """Bucket one bot AND say why it's flagged.

    Returns ``(status, reason_code, reason_detail)``. ``reason_code`` is one of
    ``"error" | "accounts" | "stale" | "quiet" | "dead" | ""`` (empty = farming);
    ``reason_detail`` is a short human string, possibly empty when the age alone
    carries the meaning. Pure; mirrors the original ``classify_status`` branch
    order so the status result is identical.
    """
    if age_min > 180:
        return DEAD, "dead", ""
    bucket = classify(text) if text else "unknown"
    acc = extract_account_count(text) if text else None
    # Only a GENUINE error (classify == "error") flags on content. "unknown"
    # chatter — a panel status line we don't have a pattern for — is NOT a red
    # flag on its own: it falls through to the freshness ladder below, exactly
    # like the live recovery path, which drops "unknown" messages
    # (mcp_watcher: analyze_unknown off). This keeps the report flagging on
    # message FRESHNESS, not unrecognized content (e.g. fresh "lobby creation"
    # is the panel working, not a problem).
    if bucket == "error":
        return ATTENTION, "error", summarize(text)
    if acc is not None and acc != 4:
        return ATTENTION, "accounts", f"accounts {acc}/4"
    if age_min > 90:
        return ATTENTION, "stale", ""
    quiet_thr = float(getattr(cfg, "quiet_threshold_minutes", 60))
    if age_min <= quiet_thr and text and farming_indicator(text):
        return FARMING, "", ""
    return QUIET, "quiet", ""


def classify_status(text, age_min, cfg):
    """Bucket one bot from its latest message text + age (minutes). Back-compat
    wrapper over ``classify_status_detailed`` — returns just the status string."""
    return classify_status_detailed(text, age_min, cfg)[0]


async def scan(client, cfg, watch):
    """Read + classify the whole watch roster.

    Returns ``{bot_num: {"pc", "status", "age_min", "name", "reason_code",
    "reason_detail"}}`` (bots without a number in their name are skipped).
    Never raises on a single bot's read.
    """
    pc_map = load_pc_map(cfg)
    now_ts = time.time()
    out = {}
    for name, ent in watch:
        try:
            text, date = await tg_tools.latest_message(client, ent, mark_read=False)
        except Exception:  # noqa: BLE001
            text, date = None, None
        age_min = ((now_ts - date.timestamp()) / 60.0) if date else 1_000_000.0
        m = _BOT_NUM_RE.search(name)
        if not m:
            continue
        bot_num = int(m.group(1))
        status, reason_code, reason_detail = classify_status_detailed(
            text, age_min, cfg)
        out[bot_num] = {
            "pc": pc_map.get(bot_num, "?"),
            "status": status,
            "age_min": age_min,
            "name": name,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
        }
    return out
