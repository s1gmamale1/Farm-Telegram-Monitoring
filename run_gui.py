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
_running = True


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


def read_all_bots(cfg):
    """Activate Telegram and scroll the chat list to the END, enumerating EVERY
    bot chat by its title row (reliable — every chat has a title), and pairing
    each with its latest preview line (for change detection). Also collects the
    set of bots currently showing an unread badge.

    Returns (bounds, {bot_key: preview_text}, {unread_bot_keys}).
    """
    g.activate()
    bounds = g.window_bounds()
    if not bounds:
        log.warning("Telegram window not found/visible — skipping this scan.")
        return None, {}, set()
    _wid, x, y, w, h = bounds
    sx, sy = x + w * 0.25, y + h * 0.5     # pointer over the chat list

    g.scroll_to_top(sx, sy)
    by_bot = {}                             # bot key -> latest preview text
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
            by_bot[key] = preview
        stale = stale + 1 if len(by_bot) == before else 0
        if stale >= 3 or len(by_bot) >= cfg.gui_max_bots:
            break
        g.scroll(sx, sy, -6)               # page down a little
    return bounds, by_bot, unread


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


def find_chat_click(frags, chat_name):
    """Return (x, y) of the sidebar row to click to open `chat_name`, or None."""
    name = chat_name.lower()
    sidebar, _ = g.split_columns(frags)
    # Prefer a fragment that IS the name (not a "[tag] ..." preview line).
    cands = [f for f in sidebar if name in f.text.lower() and not f.text.strip().startswith("[")]
    if not cands:
        cands = [f for f in sidebar if name in f.text.lower()]
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
        target = find_chat_click(fresh, chat_name)
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
    again = find_chat_click(fresh, chat_name)
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


def gui_send(cfg, bounds, text_line):
    """Open the alert chat and paste+send a one-line message. Returns bool.

    Uses clipboard paste (reliable) rather than per-character typing, and the
    configured send shortcut. Re-reads the screen fresh (the sidebar reorders as
    messages arrive) so we never click a stale position.
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
    g.set_clipboard(text_line)
    g.paste()
    time.sleep(0.6)                 # let the pasted text settle before sending
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
    bounds, previews, unread = read_all_bots(cfg)
    if bounds is None:
        return
    now = time.time()
    state["check_count"] = state.get("check_count", 0) + 1
    first = not state.get("_seeded")

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

    # Deep-read + evaluate only the changed chats.
    for bot in changed:
        if cfg.gui_read_mode == "sidebar":
            text = previews.get(bot, "")
        else:
            text = deep_read_chat(cfg, bounds, bot) or previews.get(bot, "")
        log.debug("deep-read %s -> %r", bot, text[:60])
        _evaluate_bot(cfg, store, state, bounds, bot, text, now, deliver)

    # Active issue if ANY bot (changed or not) is currently flagged in error.
    active_issue = any(state.get(b + "::err") for b in previews)

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


def answer_replies(cfg, state, deliver):
    """Check the alert chat for a new incoming reply; if found, have Hermes
    answer it and type the reply back. Shares `state` with scan_once (so the
    'all good' status logic sees Hermes's sends). Returns True if it answered."""
    if not (cfg.hermes_enabled and deliver):
        return False
    g.activate()
    bounds = g.window_bounds()
    if not bounds:
        return False
    if not open_chat_by_name(cfg, bounds, cfg.gui_alert_chat):
        return False

    frags = g.ocr_window(bounds)
    _sb, convo = g.split_columns(frags)
    # Incoming (reply) bubbles hug the LEFT of the right pane; our own messages
    # hug the right. Ignore the header (very top) and input row (very bottom).
    incoming = [
        f for f in convo
        if 0.10 < f.ny < 0.88 and (f.nx + f.nw / 2.0) < cfg.gui_incoming_x_threshold
    ]
    if not incoming:
        return False
    newest = max(incoming, key=lambda f: f.cy)   # bottom-most = newest
    reply_text = newest.text.strip()
    if len(reply_text) < 2:
        return False

    # Loop guard: never react to our own messages (alerts/status say "WatcherDog";
    # last sent is tracked) even if x-classification misreads a bubble.
    low = reply_text.lower()
    if "watcherdog" in low or low == state.get("last_sent_to_ibo", ""):
        return False

    h = error_hash(reply_text)
    if "reply_last" not in state:
        # First look: remember the current latest message, don't answer backlog.
        state["reply_last"] = h
        return False
    if state.get("reply_last") == h:
        return False  # already answered this reply
    state["reply_last"] = h

    log.info("Reply detected in %r: %r — asking Hermes…", cfg.gui_alert_chat, reply_text[:60])
    # Pop open a Terminal tailing the live Hermes conversation (if not already open).
    hermes_bridge.ensure_terminal(cfg.hermes_chat_log, enabled=cfg.hermes_terminal)
    answer = hermes_bridge.ask_hermes(
        reply_text, hermes_bin=cfg.hermes_bin, session=cfg.hermes_session,
        timeout=cfg.hermes_timeout, log_path=cfg.hermes_chat_log,
    )
    if not answer:
        log.warning("Hermes gave no answer; skipping.")
        return False
    # one-line for reliable GUI sending
    answer_line = " ".join(answer.split())[:1500]
    ok = gui_send(cfg, bounds, answer_line)
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    cfg = load_config()
    g.set_smooth(cfg.gui_smooth_input)
    store = IncidentStore(cfg.db_path)
    state = {}

    log.info(
        "GUI watcher | mode=%s | poll=%.0fs | min_severity=%s | alert_chat=%r | SEND=%s | hermes=%s | smooth=%s | unread_only=%s",
        cfg.gui_read_mode, cfg.gui_poll_interval, cfg.min_severity, cfg.gui_alert_chat,
        "ON" if cfg.gui_send_enabled else "OFF (dry-run)",
        "ON" if cfg.hermes_enabled else "off",
        "ON" if cfg.gui_smooth_input else "off",
        "ON" if cfg.gui_unread_only else "off",
    )

    try:
        if args.once:
            scan_once(cfg, store, state, cfg.gui_send_enabled)
            answer_replies(cfg, state, cfg.gui_send_enabled)
            return 0
        while _running:
            try:
                scan_once(cfg, store, state, cfg.gui_send_enabled)
                answer_replies(cfg, state, cfg.gui_send_enabled)
            except Exception:  # noqa: BLE001
                log.exception("cycle failed; continuing")
            slept = 0.0
            while _running and slept < cfg.gui_poll_interval:
                time.sleep(min(1.0, cfg.gui_poll_interval - slept))
                slept += 1.0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
