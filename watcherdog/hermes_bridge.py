"""Bridge to the local Hermes agent for the two-way conversation.

Calls `hermes -z "<prompt>" --continue <session>` so the agent answers with
context carried across turns, and returns its reply text to be typed back into
the chat. Hermes uses local Ollama under the hood and can take actions, so it
"changes accordingly" as the conversation develops.
"""

from __future__ import annotations

import logging
import os
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


def ensure_terminal(log_path, *, enabled=True):
    """Open a Terminal window tailing `log_path` (the live Hermes conversation)
    if one isn't already open. No-op when disabled. Safe to call every cycle."""
    if not (enabled and log_path):
        return
    # Already watching? Look for our tail process by the log path.
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"tail -f {log_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if found.returncode == 0 and found.stdout.strip():
            return  # a Terminal is already tailing it
    except Exception as exc:  # noqa: BLE001
        logger.debug("pgrep check failed: %s", exc)

    # Make sure the file exists so `tail` doesn't error on a fresh start.
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        open(log_path, "a", encoding="utf-8").close()
    except OSError as exc:
        logger.debug("Could not create hermes chat log %s: %s", log_path, exc)
        return

    script = (
        'tell application "Terminal"\n'
        f'  do script "printf \'\\\\033]0;Hermes (watcherdog)\\\\007\'; '
        f'tail -f {log_path}"\n'
        '  activate\n'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        logger.info("Opened Terminal tailing Hermes chat log: %s", log_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open Hermes Terminal: %s", exc)


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


def prime_incident(summary, *, hermes_bin, session, timeout=180.0):
    """Tell Hermes about a freshly-detected incident so the session has context
    when the user replies. Returns Hermes's acknowledgement (ignored if None)."""
    prompt = (
        "You are monitoring CS2 farming bots and chatting with the owner about "
        "incidents. Context for the conversation that follows: " + summary +
        " Acknowledge in one short line; do not over-explain."
    )
    return ask_hermes(prompt, hermes_bin=hermes_bin, session=session, timeout=timeout)
