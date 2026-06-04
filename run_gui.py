#!/usr/bin/env python3
"""WatcherDogBot — GUI (no-API) mode.

Drives the real Telegram macOS app like a person: screenshots the window, reads
it with on-device OCR, detects problems with the local Ollama brain, and types
an alert to a chosen chat. No api_id, no bot token, no Telegram API at all.

Reads each bot's latest message from the chat-list sidebar every cycle (so it
doesn't have to click into 20+ chats), and uses preview changes as heartbeats
for silence detection.

    python3 run_gui.py --once     # one scan, print what it detects (great for testing)
    python3 run_gui.py            # run continuously

SAFETY: set GUI_SEND_ENABLED=true in .env to actually type/send alerts. While
false (default) it detects and logs but never types into Telegram.

Requirements: Screen Recording + Accessibility permissions; Telegram visible.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

from watcherdog import gui_mac as g
from watcherdog.alerter import (
    format_alert_oneline,
    format_recovery_oneline,
    format_silence_oneline,
)
from watcherdog import hermes_bridge
from watcherdog.analyzer import analyze_message
from watcherdog.classifier import bot_name_from, classify
from watcherdog.config import SEVERITY_ORDER, load_config
from watcherdog.heartbeat import HeartbeatMonitor
from watcherdog.monitor import error_hash
from watcherdog.storage import IncidentStore

log = logging.getLogger("watcherdog.gui")

_TAG_RE = re.compile(r"\[[^\]]+\]")  # a "[SinFermeraN]" style tag => a status preview
_BADGE_RE = re.compile(r"^\d{1,4}$")  # a digits-only sidebar fragment => unread count badge
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")
_DATE_HINT = re.compile(
    r"\b(yesterday|mon|tue|wed|thu|fri|sat|sun|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|\d{1,2}[/.]\d{1,2}", re.I)
_running = True


def parse_age_minutes(time_str, now):
    """Best-effort age (minutes) of a sidebar/message time string. Returns a
    big number for a non-today date, 0 for a near-future clock skew, or None if
    unparseable (caller treats None as 'recent', i.e. don't skip)."""
    if not time_str:
        return None
    s = time_str.strip()
    if _DATE_HINT.search(s):
        return 100000.0                      # not today -> stale
    m = _TIME_RE.search(s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower()
    if ap.startswith("p") and hh != 12:
        hh += 12
    elif ap.startswith("a") and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    lt = time.localtime(now)
    cand = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    age = (now - time.mktime(cand)) / 60.0
    return 0.0 if age < -5 else age


def _status_rows_from(sidebar, gap_px):
    """Extract '[SinFermeraN] ...' preview lines from sidebar fragments,
    gluing a wrapped continuation line when present."""
    sidebar = sorted(sidebar, key=lambda f: f.cy)
    rows = []
    for i, f in enumerate(sidebar):
        if not _TAG_RE.search(f.text):
            continue
        text = f.text.strip()
        if i + 1 < len(sidebar):
            nxt = sidebar[i + 1]
            if (0 < (nxt.cy - f.cy) <= gap_px
                    and abs(nxt.cx - f.cx) < 120
                    and not _TAG_RE.search(nxt.text)):
                text = f"{text} {nxt.text.strip()}"
        rows.append(text)
    return rows


def _unread_bot_keys(sidebar, cfg):
    """Bot keys whose chat row shows a numeric unread badge.

    Telegram draws the unread count as a small rounded badge at the far right
    of the row; OCR sees it as a digits-only fragment. A bot chat is "unread"
    when such a fragment sits to the right of its title on the same row.
    """
    if not sidebar:
        return set()
    min_cx = min(f.cx for f in sidebar)
    max_cx = max(f.cx for f in sidebar)
    span = max(1.0, max_cx - min_cx)
    badges = [
        f for f in sidebar
        if _BADGE_RE.match(f.text.strip())
        and (f.cx - min_cx) / span >= cfg.gui_unread_x_frac
    ]
    if not badges:
        return set()
    titles = [
        (f, _bot_key(f.text, cfg.gui_bot_name_re))
        for f in sidebar
        if _bot_key(f.text, cfg.gui_bot_name_re)
    ]
    unread = set()
    for b in badges:
        best_key, best_d = None, 1e9
        for tf, key in titles:
            if tf.cx >= b.cx:                       # title must be left of badge
                continue
            d = abs(tf.cy - b.cy)
            if d < best_d and d <= cfg.gui_row_gap_px * 2:
                best_key, best_d = key, d
        if best_key:
            unread.add(best_key)
    return unread


def select_folder(bounds, name):
    """Click a chat-list folder tab (e.g. 'Unread', 'All Chats') in the far-left
    folder column. Returns True if found+clicked."""
    want = name.lower().replace(" ", "")
    frags = g.ocr_window(bounds)
    cands = [
        f for f in frags
        if want in f.text.lower().replace(" ", "")
        and (f.nx + f.nw / 2.0) < 0.12          # the narrow folder column
    ]
    if not cands:
        return False
    t = min(cands, key=lambda f: f.cy)
    g.click(t.cx, t.cy, settle=0.8)
    return True


def read_all_bots(cfg, use_unread=True):
    """Activate Telegram and scroll the chat list to the END, enumerating EVERY
    bot chat by its title row (reliable — every chat has a title), and pairing
    each with its latest preview line (for change detection). Also collects the
    set of bots currently showing an unread badge.

    `use_unread=False` reads the full "All Chats" list (used once on launch to
    baseline every bot); True filters to the "Unread" folder thereafter.

    Returns (bounds, {bot_key: preview_text}, {unread_bot_keys}, {bot_key: time}).
    """
    g.activate()
    bounds = g.window_bounds()
    if not bounds:
        log.warning("Telegram window not found/visible — skipping this scan.")
        return None, {}, set(), {}
    _wid, x, y, w, h = bounds
    sx, sy = x + w * 0.25, y + h * 0.5     # pointer over the chat list

    # Filter the chat list to the built-in "Unread" folder so we see ONLY chats
    # with new messages — far more reliable than reading the tiny unread badges
    # (which OCR can't). We restore "All Chats" before returning.
    folder_filtered = False
    if cfg.gui_unread_only and use_unread:
        folder_filtered = select_folder(bounds, "Unread")

    g.scroll_to_top(sx, sy)
    by_bot = {}                             # bot key -> latest preview text
    times = {}                              # bot key -> last-message time string
    unread = set()                          # bot keys with an unread badge
    stale = 0
    for _ in range(cfg.gui_scroll_max):
        frags = g.ocr_window(bounds)
        sidebar, _convo = g.split_columns(frags)
        sidebar.sort(key=lambda f: f.cy)
        unread |= _unread_bot_keys(sidebar, cfg)
        before = len(by_bot)
        for i, f in enumerate(sidebar):
            if f.text.strip().startswith("["):
                continue
            key = _bot_key(f.text, cfg.gui_bot_name_re)
            if not key or key in by_bot:
                continue
            # preview = the next line just below the title (same chat row)
            preview = ""
            for nxt in sidebar[i + 1:]:
                if 0 < (nxt.cy - f.cy) <= 44 and abs(nxt.cx - f.cx) < 220:
                    preview = nxt.text.strip()
                    break
            # time = a time/date-looking fragment on the SAME row, to the right
            tstr = ""
            for other in sidebar:
                if abs(other.cy - f.cy) <= 16 and (other.nx + other.nw / 2.0) > 0.30:
                    if _TIME_RE.search(other.text) or _DATE_HINT.search(other.text):
                        tstr = other.text.strip()
                        break
            by_bot[key] = preview
            times[key] = tstr
        stale = stale + 1 if len(by_bot) == before else 0
        if stale >= 3 or len(by_bot) >= cfg.gui_max_bots:
            break
        g.scroll(sx, sy, -6)               # page down a little

    if folder_filtered:
        # Every chat in the Unread folder is, by definition, unread.
        unread = set(by_bot)
        select_folder(bounds, "All Chats")   # restore the normal view
    return bounds, by_bot, unread, times


def _bot_key(text, pattern):
    """Normalized bot id from a chat-title row (e.g. 'SinFermera18 *' -> 'sinfermera18').
    Canonicalizes the common OCR variance 'SinFarmera' -> 'SinFermera' so a bot
    is never counted twice."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    key = re.sub(r"\s+", "", m.group(0)).lower()
    return key.replace("farmera", "fermera")


def latest_convo_message(convo, incoming_x_threshold):
    """Return the bot's most recent message text from an opened chat.

    In a bot DM almost every message is the bot's, so we don't filter by
    left/right here (that wrongly drops wide lines). Starting from the
    bottom-most line, walk upward gathering vertically-contiguous lines
    (gaps < ~34px) so a multi-line message is captured whole.
    """
    msgs = [f for f in convo if 0.10 < f.ny < 0.86]
    if not msgs:
        return ""
    msgs.sort(key=lambda f: f.cy)            # top -> bottom
    cluster = [msgs[-1]]
    prev_cy = msgs[-1].cy
    for f in reversed(msgs[:-1]):
        if prev_cy - f.cy <= 34:             # same message block
            cluster.append(f)
            prev_cy = f.cy
        else:
            break
    cluster.sort(key=lambda f: (round(f.cy / 6), f.cx))   # reading order
    return " ".join(f.text.strip() for f in cluster).strip()


def read_each_chat(cfg):
    """Open every bot chat in turn, read its latest message, and return
    (bounds, {bot_key: latest_message_text}). Scrolls the list to the end so all
    bots (up to GUI_MAX_BOTS) are covered."""
    g.activate()
    bounds = g.window_bounds()
    if not bounds:
        log.warning("Telegram window not found/visible — skipping this scan.")
        return None, {}
    _wid, x, y, w, h = bounds
    sx, sy = x + w * 0.25, y + h * 0.5

    g.scroll_to_top(sx, sy)
    results = {}
    stale = 0
    for _ in range(cfg.gui_scroll_max):
        if len(results) >= cfg.gui_max_bots or stale >= 4:
            break
        frags = g.ocr_window(bounds)
        sidebar, _convo = g.split_columns(frags)
        # chat-title rows for bots (not the "[tag] preview" lines)
        rows = []
        for f in sorted(sidebar, key=lambda f: f.cy):
            if f.text.strip().startswith("["):
                continue
            key = _bot_key(f.text, cfg.gui_bot_name_re)
            if key and key not in results and key not in [r[0] for r in rows]:
                rows.append((key, f.cx, f.cy))
        clicked = 0
        for key, cx, cy in rows:
            if key in results:
                continue
            g.click(cx, cy, settle=cfg.gui_chat_load_wait)
            cf = g.ocr_window(bounds)
            _s, convo = g.split_columns(cf)
            results[key] = latest_convo_message(convo, cfg.gui_incoming_x_threshold)
            clicked += 1
            log.debug("read chat %s -> %r", key, results[key][:50])
            if len(results) >= cfg.gui_max_bots:
                break
        stale = stale + 1 if clicked == 0 else 0
        g.scroll(sx, sy, -8)
    log.info("Read %d bot chats by opening each.", len(results))
    return bounds, results


def find_chat_click(frags, chat_name, header_band=0.10):
    """Return (x, y) of the chat-list row to click to open `chat_name`, or None.

    When the chat is already open, its name ALSO appears in the conversation
    HEADER at the very top of the window — and that title sits around nx≈0.5, so
    it leaks into the 'sidebar' half. Clicking the header title opens the
    contact's PROFILE panel instead of selecting the chat. We therefore ignore
    any match in the top `header_band` fraction of the window: the header lives
    there, while the chat list's first row is always below the search box. Among
    the remaining chat-list matches we take the topmost (the list is newest-first).
    """
    name = chat_name.lower()
    sidebar, _ = g.split_columns(frags)
    # f.ny is bottom-left-origin normalized, so (1 - center_y) is the distance
    # from the TOP of the window; drop anything inside the header band.
    rows = [f for f in sidebar if (1.0 - (f.ny + f.nh / 2.0)) >= header_band]
    # Prefer a fragment that IS the name (not a "[tag] ..." preview line).
    cands = [f for f in rows if name in f.text.lower() and not f.text.strip().startswith("[")]
    if not cands:
        cands = [f for f in rows if name in f.text.lower()]
    if not cands:
        return None
    # topmost match (chat list is newest-first)
    best = min(cands, key=lambda f: f.cy)
    return best.cx, best.cy


def open_chat_by_name(cfg, bounds, chat_name):
    """Find a chat by name anywhere in the list (scrolling from the top) and
    open it. Returns True if opened. Needed because after reading 24 bots the
    list is scrolled to the bottom and the alert chat may be off-screen."""
    _wid, x, y, w, h = bounds
    sx, sy = x + w * 0.25, y + h * 0.5
    g.scroll_to_top(sx, sy)
    for _ in range(max(2, cfg.gui_scroll_max)):
        fresh = g.ocr_window(bounds)
        target = find_chat_click(fresh, chat_name, cfg.gui_header_band_frac)
        if target:
            g.click(target[0], target[1], settle=1.0)
            return True
        g.scroll(sx, sy, -8)
    return False


def deep_read_chat(cfg, bounds, chat_name):
    """Open a chat, TAP IT AGAIN (jump to the latest message) and scroll the
    conversation to the bottom, then read the most recent message text."""
    if not open_chat_by_name(cfg, bounds, chat_name):
        return ""
    _wid, x, y, w, h = bounds
    # Second tap on the same chat row jumps to the newest message (Telegram
    # opens at the first *unread* otherwise, so the latest can be off-screen).
    fresh = g.ocr_window(bounds)
    again = find_chat_click(fresh, chat_name, cfg.gui_header_band_frac)
    if again:
        g.click(again[0], again[1], settle=cfg.gui_chat_load_wait)
    # Make sure the conversation is scrolled to the very bottom.
    g.scroll(x + w * 0.72, y + h * 0.5, -8)
    cf = g.ocr_window(bounds)
    _s, convo = g.split_columns(cf)
    return latest_convo_message(convo, cfg.gui_incoming_x_threshold)


def gui_edit_status(cfg, bounds, new_text):
    """Edit our LAST message in the alert chat in place (instead of sending a
    new one) — used to bump the 'checks done N times' counter. Returns True if
    the edit was saved, False if we couldn't enter edit mode (caller falls back
    to sending fresh)."""
    g.activate()
    if not open_chat_by_name(cfg, bounds, cfg.gui_alert_chat):
        return False
    _wid, x, y, w, h = bounds
    frags = g.ocr_window(bounds)
    inp = [f for f in frags if "write a message" in f.text.lower()]
    ix, iy = (inp[0].cx, inp[0].cy) if inp else (x + w * cfg.gui_input_x_frac, y + h * 0.955)
    g.click(ix, iy, settle=0.4)
    g.clear_input()                      # composer must be empty so Up = edit
    time.sleep(0.2)
    g.press_key(126)                     # Up arrow -> edit your last message
    time.sleep(0.7)
    chk = g.ocr_window(bounds)
    if not any("edit" in f.text.lower() for f in chk):
        g.press_escape()                 # didn't enter edit mode
        return False
    g.press_key_cmd(0)                   # Cmd+A select existing text
    time.sleep(0.1)
    g.set_clipboard(new_text)
    g.paste()
    time.sleep(0.3)
    if cfg.gui_send_key == "cmd_return":
        g.send_cmd_return()
    else:
        g.send_plain_return()
    time.sleep(0.5)
    return True


def gui_send(cfg, bounds, text_line, *, human=False):
    """Open the alert chat and send a one-line message. Returns bool.

    `human=True` types the text character-by-character with human cadence (used
    for conversational replies so it looks like a real person); otherwise it
    pastes (faster/robust, used for status + alerts). Re-reads the screen fresh
    (the sidebar reorders as messages arrive) so we never click a stale position.
    """
    g.activate()
    if not open_chat_by_name(cfg, bounds, cfg.gui_alert_chat):
        log.error("Alert chat %r not found (scrolled the list) — cannot send.", cfg.gui_alert_chat)
        return False

    # Focus the RIGHT-pane message input (placeholder if visible, else fraction).
    frags2 = g.ocr_window(bounds)
    inp = [f for f in frags2 if "write a message" in f.text.lower()]
    _wid, x, y, w, h = bounds
    if inp:
        ix, iy = inp[0].cx, inp[0].cy
    else:
        ix, iy = x + w * cfg.gui_input_x_frac, y + h * 0.955
    g.click(ix, iy, settle=0.4)

    g.clear_input()                 # drop any leftover draft
    time.sleep(0.2)
    if human:
        g.type_text(text_line)      # human cadence, looks like real typing
    else:
        g.set_clipboard(text_line)
        g.paste()
    time.sleep(0.6)                 # let the text settle before sending
    if cfg.gui_send_key == "cmd_return":
        g.send_cmd_return()
    else:
        g.send_plain_return()
    time.sleep(0.7)

    # Verify the send: a successful send clears the input, so the placeholder
    # "Write a message..." returns and the text is no longer sitting at the
    # bottom input row (very low ny).
    after = g.ocr_window(bounds)
    snippet = text_line[:18].lower()
    placeholder_back = any("write a message" in f.text.lower() for f in after)
    in_input_row = any(snippet in f.text.lower() and f.ny < 0.085 for f in after)
    ok = placeholder_back and not in_input_row
    log.info("Pasted+sent alert to %r (verified_sent=%s)", cfg.gui_alert_chat, ok)
    return ok


def _send_to_ibo(cfg, bounds, state, line):
    """Send a line to the alert chat. Only remember it as 'last sent' on success
    (so a failed send is retried next cycle, not silently treated as done)."""
    ok = gui_send(cfg, bounds, line)
    if ok:
        state["last_sent_to_ibo"] = line.lower()
    return ok


def _evaluate_bot(cfg, store, state, bounds, bot, text, now, deliver):
    """Classify + (Ollama) analyze one bot's latest message; alert on a real
    error. Updates state[bot+'::err']."""
    if not text or not text.strip():
        return
    h = error_hash(text)
    bucket = classify(text)
    if bucket == "normal":
        state[bot + "::err"] = False
        return
    if bucket == "unknown" and not cfg.analyze_unknown:
        state[bot + "::err"] = False
        return

    analysis = analyze_message(
        text, bot_name=bot, ollama_url=cfg.ollama_url,
        model=cfg.ollama_model, timeout=cfg.ollama_timeout,
    )
    if not analysis.get("is_error"):
        state[bot + "::err"] = False
        return

    severity = analysis.get("severity", "high")
    if SEVERITY_ORDER.get(severity, 2) < SEVERITY_ORDER[cfg.min_severity]:
        state[bot + "::err"] = False
        store.record(bot, severity, analysis, h, text, notified=False, ts=now)
        log.info("below-threshold: %s (%s)", bot, severity)
        return

    state[bot + "::err"] = True
    line = format_alert_oneline(bot, severity, analysis)
    recent = store.last_seen(h)
    if recent is not None and (now - recent) < cfg.dedupe_window:
        log.info("error on %s already alerted recently; not resending", bot)
        return

    if deliver:
        ok = _send_to_ibo(cfg, bounds, state, line)
        if ok:
            state["status_active"] = False   # a non-status message is now last
        if cfg.hermes_enabled:
            summary = (f"{bot} ({severity}): {analysis.get('summary','')}. "
                       f"Suggested fix: {analysis.get('fix','')}")
            hermes_bridge.prime_incident(
                summary, hermes_bin=cfg.hermes_bin, session=cfg.hermes_session,
                timeout=cfg.hermes_timeout,
            )
    else:
        ok = False
        log.info("[DRY-RUN] would alert: %s", line)
    store.record(bot, severity, analysis, h, text, notified=ok, ts=now)


def check_silence(cfg, state, bounds, previews, times, now, deliver, first):
    """Pull-mode silence detection (wishlist High-priority).

    Each chat's last-message time is already OCR'd into `times`; we turn it into
    an age and:
      * alert ONCE when a bot's latest message is older than the silence
        threshold (it may be down / banned / stalled), and
      * announce recovery when a previously-silent bot speaks again.

    On the FIRST scan we only SEED each bot's silent flag (no alert): a restart,
    or bots simply quiet overnight, shouldn't trigger a flood. Already-silent
    bots are logged so the information isn't lost. This mirrors the heartbeat
    monitor's grace period.
    """
    if not cfg.silence_enabled:
        return
    threshold_min = cfg.silence_threshold / 60.0
    already_silent = []
    for bot in previews:
        age = parse_age_minutes(times.get(bot, ""), now)
        if age is None:
            continue                       # unknown age -> treat as recent
        silent = age > threshold_min
        key = bot + "::silent"
        was_silent = state.get(key, False)
        if first:
            state[key] = silent
            if silent:
                already_silent.append(bot)
            continue
        if silent and not was_silent:
            line = format_silence_oneline(bot, age * 60.0)
            if deliver:
                ok = _send_to_ibo(cfg, bounds, state, line)
                if ok:
                    state["status_active"] = False
            else:
                log.info("[DRY-RUN] would alert silence: %s", line)
            state[key] = True
            log.info("SILENT: %s (last message ~%.0fm ago)", bot, age)
        elif not silent and was_silent:
            if deliver:
                ok = _send_to_ibo(cfg, bounds, state, format_recovery_oneline(bot))
                if ok:
                    state["status_active"] = False
            else:
                log.info("[DRY-RUN] would alert recovery: %s", bot)
            state[key] = False
            log.info("RECOVERED: %s is posting again", bot)
    if first and already_silent:
        log.info("Seeding silence state; already quiet at startup (not alerting): %s",
                 ", ".join(already_silent))


def scan_once(cfg, store, state, deliver):
    """One scan cycle.

    1. Read every bot's chat-list preview (fast pass).
    2. Deep-read ONLY the chats whose preview changed (a new message) — opening
       each, tapping again to jump to the latest message, reading it in full.
       This skips already-read chats and never misses new-message chats.
    3. Ollama decides; real errors → alert ibo.
    4. All good → EDIT the running status message with the check count
       (e.g. "…everything working perfectly. Checks done: 67 times.") rather
       than sending a new message each time.
    """
    # FIRST launch: read the full "All Chats" list to baseline every bot.
    # AFTER that: watch only the "Unread" folder (chats with new messages).
    first = not state.get("_seeded")
    bounds, previews, unread, times = read_all_bots(cfg, use_unread=not first)
    if bounds is None:
        return
    now = time.time()
    state["check_count"] = state.get("check_count", 0) + 1

    # Track which previews changed since last cycle (kept for the fallback path
    # and as a baseline on the very first scan).
    hash_changed = []
    for bot, prev in previews.items():
        ph = error_hash(prev)
        if first or state.get(bot + "::prev") != ph:
            hash_changed.append(bot)
        state[bot + "::prev"] = ph
    state["_seeded"] = True

    # Decide which chats to deep-read this cycle:
    #   first scan        -> read everything once to baseline
    #   unread detection  -> only chats flagged unread (+ optional hash fallback)
    #   otherwise         -> preview-text-change detection (legacy behaviour)
    if first:
        changed = list(previews)
    elif cfg.gui_unread_only:
        sel = set(u for u in unread if u in previews)
        if cfg.gui_unread_fallback:
            sel |= set(hash_changed)
        changed = sorted(sel)
    else:
        changed = hash_changed
    log.info("Check #%d: %d chats, %d unread, %d to read%s",
             state["check_count"], len(previews), len(unread & set(previews)),
             len(changed), (": " + ", ".join(changed)) if changed else "")

    # Deep-read + evaluate only the changed chats — but skip anything whose last
    # message is older than the max age (stale; not worth acting on).
    for bot in changed:
        age = parse_age_minutes(times.get(bot, ""), now)
        if age is not None and age > cfg.gui_max_age_minutes:
            log.debug("skip %s: last message ~%.0fm old (> %.0fm)",
                      bot, age, cfg.gui_max_age_minutes)
            state[bot + "::err"] = False
            continue
        if cfg.gui_read_mode == "sidebar":
            text = previews.get(bot, "")
        else:
            text = deep_read_chat(cfg, bounds, bot) or previews.get(bot, "")
        log.debug("deep-read %s -> %r", bot, text[:60])
        _evaluate_bot(cfg, store, state, bounds, bot, text, now, deliver)

    # Silence detection (pull mode): alert on bots whose last message is older
    # than the silence threshold, recover when they speak again.
    check_silence(cfg, state, bounds, previews, times, now, deliver, first)

    # Active issue if ANY bot (changed or not) is currently flagged in error or
    # has gone silent — either way we must NOT claim "everything working".
    active_issue = any(state.get(b + "::err") or state.get(b + "::silent")
                       for b in previews)

    if active_issue:
        return

    # All good → keep ONE status message, edited with the running check count.
    status = f"{cfg.gui_status_message} Checks done: {state['check_count']} times."
    if not deliver:
        log.info("[DRY-RUN] all good — status would read: %s", status)
        state["status_active"] = True
        return
    if state.get("status_active"):
        ok = gui_edit_status(cfg, bounds, status)
        if not ok:                       # couldn't edit → send a fresh one
            ok = _send_to_ibo(cfg, bounds, state, status)
        log.info("All good — status edited (checks=%d, ok=%s)", state["check_count"], ok)
    else:
        ok = _send_to_ibo(cfg, bounds, state, status)
        log.info("All good — status sent (checks=%d, ok=%s)", state["check_count"], ok)
    state["status_active"] = bool(ok)


def find_reply(cfg, convo):
    """Pick the message to answer from the open conversation pane.

    Two detection paths, most-reliable first (wishlist High-priority items
    'robust reply detection' + 'detect your reply from the same account'):

      1. COMMAND PREFIX (e.g. '!dog ...') anywhere in the pane. This is the
         reliable path: a message you type from the SAME account WatcherDog
         watches from renders as OUTGOING (right side), so the left/right
         x-heuristic alone can never see it. A prefixed message is matched no
         matter which side it lands on.
      2. INCOMING bubble heuristic — a fragment hugging the LEFT of the right
         pane (a reply from someone else). Best-effort; position/OCR only.

    Returns (raw_text, question_text): `raw_text` drives de-dup (so the same
    message isn't answered twice) and `question_text` is what to ask Hermes
    (command prefix stripped). Returns (None, None) when there's nothing to do.
    """
    # Ignore the header (very top) and the input row (very bottom).
    pane = [f for f in convo if 0.10 < f.ny < 0.88]
    if not pane:
        return None, None

    prefix = (cfg.gui_command_prefix or "").lower()
    if prefix:
        cmds = [f for f in pane if f.text.strip().lower().startswith(prefix)]
        if cmds:
            newest = max(cmds, key=lambda f: f.cy)      # bottom-most = newest
            raw = newest.text.strip()
            question = raw[len(cfg.gui_command_prefix):].strip(" :,-—")
            if question:
                return raw, question

    incoming = [f for f in pane if (f.nx + f.nw / 2.0) < cfg.gui_incoming_x_threshold]
    if not incoming:
        return None, None
    newest = max(incoming, key=lambda f: f.cy)          # bottom-most = newest
    raw = newest.text.strip()
    if len(raw) < 2:
        return None, None
    return raw, raw


def answer_replies(cfg, state, deliver):
    """Check the alert chat for a new reply/command; if found, have Hermes
    answer it and type the reply back. Shares `state` with scan_once (so the
    'all good' status logic sees Hermes's sends). Returns True if it answered."""
    if not (cfg.hermes_enabled and deliver):
        return False
    g.activate()
    bounds = g.window_bounds()
    if not bounds:
        return False
    if not open_chat_by_name(cfg, bounds, cfg.gui_alert_chat):
        log.debug("ibo check: chat %r not found in list", cfg.gui_alert_chat)
        return False

    frags = g.ocr_window(bounds)
    _sb, convo = g.split_columns(frags)
    reply_text, question = find_reply(cfg, convo)
    if not reply_text:
        return False
    log.info("ibo check: candidate reply = %r", reply_text[:60])

    # Loop guard: never react to our own messages (alerts/status say "WatcherDog";
    # last sent is tracked) even if x-classification misreads a bubble.
    low = reply_text.lower()
    if "watcherdog" in low or low == state.get("last_sent_to_ibo", ""):
        return False

    h = error_hash(reply_text)
    if "reply_last" not in state:
        # First look after (re)start: answer the message only if it's RECENT
        # (you just texted); otherwise seed it as backlog so old messages aren't
        # answered on every restart.
        age = parse_age_minutes(reply_text, time.time())
        if age is not None and age > 15:
            state["reply_last"] = h
            log.info("ibo check: seeded old backlog (~%.0fm) — not answering", age)
            return False
        log.info("ibo check: recent message on first look — answering it")
    elif state.get("reply_last") == h:
        return False  # already answered this reply
    state["reply_last"] = h

    log.info("Reply detected in %r: %r — asking Hermes…", cfg.gui_alert_chat, question[:60])
    # Pop open a Terminal tailing the live Hermes conversation (if not already open).
    hermes_bridge.ensure_terminal(cfg.hermes_chat_log, enabled=cfg.hermes_terminal)
    answer = hermes_bridge.ask_hermes(
        question, hermes_bin=cfg.hermes_bin, session=cfg.hermes_session,
        timeout=cfg.hermes_timeout, log_path=cfg.hermes_chat_log,
    )
    if not answer:
        log.warning("Hermes gave no answer; skipping.")
        return False
    # one-line for reliable GUI sending; type it like a real person
    answer_line = " ".join(answer.split())[:1500]
    ok = gui_send(cfg, bounds, answer_line, human=cfg.gui_human_typing)
    state["last_sent_to_ibo"] = answer_line.lower()  # loop guard + status de-dup
    if ok:
        state["status_active"] = False   # status is no longer the last message
    log.info("Hermes replied in %r (sent=%s)", cfg.gui_alert_chat, ok)
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="WatcherDogBot GUI (no-API) watcher")
    parser.add_argument("--once", action="store_true", help="one scan then exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()

    # Log to the console AND mirror to gui_run.log so it can be `tail -f`'d even
    # when running in the foreground (the file used to exist only via nohup).
    handlers = [logging.StreamHandler()]
    try:
        os.makedirs(os.path.dirname(cfg.gui_run_log) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(cfg.gui_run_log))
    except OSError as exc:
        print(f"Could not open log file {cfg.gui_run_log}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )
    g.set_smooth(cfg.gui_smooth_input)
    g.install_pause_hotkey(
        cfg.gui_pause_keycode,
        on_change=lambda paused: log.warning("⏸ PAUSED (press F10 to resume)" if paused
                                             else "▶ RESUMED"),
    )
    store = IncidentStore(cfg.db_path)
    state = {}

    log.info(
        "GUI watcher | mode=%s | poll=%.0fs | ibo_poll=%.0fs | min_severity=%s | "
        "alert_chat=%r | SEND=%s | hermes=%s | smooth=%s | unread_only=%s | "
        "max_age=%.0fm | F10=pause",
        cfg.gui_read_mode, cfg.gui_poll_interval, cfg.gui_reply_poll_interval,
        cfg.min_severity, cfg.gui_alert_chat,
        "ON" if cfg.gui_send_enabled else "OFF (dry-run)",
        "ON" if cfg.hermes_enabled else "off",
        "ON" if cfg.gui_smooth_input else "off",
        "ON" if cfg.gui_unread_only else "off",
        cfg.gui_max_age_minutes,
    )

    # Pop open a tidy column of Terminal windows down the right side of the
    # screen, each tailing one log (activity + the live Hermes chat), so you can
    # watch everything at a glance. Skipped for one-shot runs.
    if not args.once:
        hermes_bridge.open_monitor_terminals(
            [("WatcherDog activity", cfg.gui_run_log),
             ("Hermes chat", cfg.hermes_chat_log)],
            enabled=cfg.gui_monitor_terminals,
        )

    try:
        if args.once:
            answer_replies(cfg, state, cfg.gui_send_enabled)   # ibo replies first
            scan_once(cfg, store, state, cfg.gui_send_enabled)
            return 0
        while _running:
            try:
                # Handle ibo's replies FIRST each cycle so the conversation is
                # snappy and doesn't wait behind the (slower) bot sweep.
                answer_replies(cfg, state, cfg.gui_send_enabled)
                scan_once(cfg, store, state, cfg.gui_send_enabled)
                answer_replies(cfg, state, cfg.gui_send_enabled)
            except Exception:  # noqa: BLE001
                log.exception("cycle failed; continuing")
            # Between full sweeps, keep polling ONLY the alert chat on a short
            # interval so a message you text in ibo is read and handed to Hermes
            # within seconds — the slow 24-bot sweep still runs once per
            # gui_poll_interval.
            next_sweep = time.monotonic() + cfg.gui_poll_interval
            while _running and time.monotonic() < next_sweep:
                nap = min(cfg.gui_reply_poll_interval,
                          max(0.0, next_sweep - time.monotonic()))
                time.sleep(nap)
                if not _running:
                    break
                try:
                    answer_replies(cfg, state, cfg.gui_send_enabled)
                except Exception:  # noqa: BLE001
                    log.exception("ibo poll failed; continuing")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
