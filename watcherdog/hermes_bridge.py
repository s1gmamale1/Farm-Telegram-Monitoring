"""Bridge to the local Hermes agent for the two-way conversation.

Calls `hermes -z "<prompt>" --continue <session>` so the agent answers with
context carried across turns, and returns its reply text to be typed back into
the chat. Hermes uses local Ollama under the hood and can take actions, so it
"changes accordingly" as the conversation develops.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger("watcherdog.hermes")


def _append_log(log_path, prompt, reply):
    """Append one USER/HERMES exchange to the chat log the Terminal tails."""
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n\033[36m[ibo]\033[0m {prompt}\n")
            fh.write(f"\033[32m[hermes]\033[0m {reply}\n")
    except OSError as exc:
        logger.debug("Could not write hermes chat log %s: %s", log_path, exc)


def _terminal_already_tailing(log_path):
    """True if a `tail -f <log_path>` process is already running (so a Terminal
    window is presumably watching it). Best-effort; False on any error."""
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"tail -f {log_path}"],
            capture_output=True, text=True, timeout=5,
        )
        return found.returncode == 0 and bool(found.stdout.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("pgrep check failed: %s", exc)
        return False


def _screen_size():
    """(width, height) of the main display in pixels, via the Finder desktop
    bounds. Falls back to a common laptop size if the probe fails."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=5,
        )
        nums = [int(n) for n in re.findall(r"-?\d+", r.stdout)]
        if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
            return nums[2], nums[3]
    except Exception as exc:  # noqa: BLE001
        logger.debug("screen-size probe failed: %s", exc)
    return 1440, 900


def _right_side_bounds(count, screen=None):
    """Lay out `count` Terminal windows as a stacked column down the RIGHT side
    of the screen. Returns a list of (left, top, right, bottom) pixel tuples."""
    w, h = screen or _screen_size()
    width = max(420, int(w * 0.40))
    left = max(0, w - width)
    top_margin, bottom_margin, gap = 30, 20, 8
    usable = h - top_margin - bottom_margin - gap * max(0, count - 1)
    win_h = max(120, usable // max(1, count))
    bounds, y = [], top_margin
    for _ in range(count):
        bounds.append((left, y, w, y + win_h))
        y += win_h + gap
    return bounds


def open_log_terminal(log_path, *, title, bounds=None, activate=True):
    """Open a Terminal window tailing `log_path`, titled `title`, optionally
    positioned at `bounds` (left, top, right, bottom). No-op (returns False) if a
    Terminal is already tailing that file."""
    if not log_path or _terminal_already_tailing(log_path):
        return False
    # Make sure the file exists so `tail` doesn't error on a fresh start.
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        open(log_path, "a", encoding="utf-8").close()
    except OSError as exc:
        logger.debug("Could not create log %s: %s", log_path, exc)
        return False

    set_bounds = ""
    if bounds:
        l, t, r, b = bounds
        set_bounds = f"  set bounds of front window to {{{l}, {t}, {r}, {b}}}\n"
    activate_line = "  activate\n" if activate else ""
    script = (
        'tell application "Terminal"\n'
        f'  do script "printf \'\\\\033]0;{title}\\\\007\'; tail -f {log_path}"\n'
        f'{set_bounds}'
        f'{activate_line}'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        logger.info("Opened Terminal tailing %s", log_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open Terminal for %s: %s", log_path, exc)
        return False


def ensure_terminal(log_path, *, enabled=True):
    """Open a Terminal window tailing `log_path` (the live Hermes conversation)
    if one isn't already open. No-op when disabled. Safe to call every cycle."""
    if not (enabled and log_path):
        return
    open_log_terminal(log_path, title="Hermes (watcherdog)")


def open_monitor_terminals(logs, *, enabled=True):
    """Open a stacked column of Terminal windows down the RIGHT side of the
    screen, one per (title, log_path), each tailing its log. Skips any log a
    Terminal is already tailing, and sizes the layout for only the ones it will
    actually open. No-op when disabled."""
    if not enabled:
        return
    pending = [(t, p) for t, p in logs if p and not _terminal_already_tailing(p)]
    if not pending:
        return
    layout = _right_side_bounds(len(pending))
    for (title, path), bnds in zip(pending, layout):
        open_log_terminal(path, title=title, bounds=bnds, activate=True)


def ask_hermes(prompt, *, hermes_bin, session="watcherdog", timeout=180.0, log_path=None):
    """Send `prompt` to Hermes and return its reply text (or None on failure).

    When `log_path` is set, the prompt and reply are appended there so a tailing
    Terminal shows the live conversation.
    """
    cmd = [hermes_bin, "-z", prompt, "--continue", session]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Hermes timed out after %.0fs", timeout)
        return None
    except FileNotFoundError:
        logger.error("Hermes binary not found at %s", hermes_bin)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes call failed: %s", exc)
        return None
    if r.returncode != 0:
        logger.warning("Hermes exited %s: %s", r.returncode, (r.stderr or "")[-200:])
        return None
    reply = (r.stdout or "").strip()
    if reply:
        _append_log(log_path, prompt, reply)
    return reply or None


def prime_session(doc_paths, *, hermes_bin, session, timeout=180.0, extra=""):
    """Seed the Hermes conversation with its operating guide, once per startup.

    Tells Hermes who it is, where its Markdown guides live (so it can read them
    with its file tool), and that it has Telegram MCP read-tools available. The
    docs themselves carry the detail (folder/chat IDs, default behaviour); this
    just points Hermes at them and asks for a one-line acknowledgement so we
    don't pay for a long reply. Returns Hermes's ack (ignored if None).
    """
    paths = [p for p in (doc_paths or []) if p]
    listing = "\n".join(f"  - {p}" for p in paths)
    prompt = (
        "You are WatcherDog's Telegram assistant for the owner (who messages you "
        "from the 'ibo' chat). You have Telegram MCP tools to READ the account "
        "(folders, chats, message history) — use them to answer questions. Do NOT "
        "send Telegram messages yourself; your text reply is delivered for you. "
        "Read your operating guide now and follow it for the rest of this "
        "conversation:\n" + listing +
        ("\n" + extra if extra else "") +
        "\nAfter reading, reply with one short line: 'WatcherDog ready.'"
    )
    return ask_hermes(prompt, hermes_bin=hermes_bin, session=session, timeout=timeout)


def prime_incident(summary, *, hermes_bin, session, timeout=180.0):
    """Tell Hermes about a freshly-detected incident so the session has context
    when the user replies. Returns Hermes's acknowledgement (ignored if None)."""
    prompt = (
        "You are monitoring CS2 farming bots and chatting with the owner about "
        "incidents. Context for the conversation that follows: " + summary +
        " Acknowledge in one short line; do not over-explain."
    )
    return ask_hermes(prompt, hermes_bin=hermes_bin, session=session, timeout=timeout)
