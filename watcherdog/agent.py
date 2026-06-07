"""WatcherDog's self-contained conversation agent.

A small READ-ONLY tool-calling loop over an OpenAI-compatible chat API
(OpenRouter by default — the same deepseek model Hermes uses). When ibo asks a
question, the model can call a handful of Telegram *read* tools (backed by the
watcher's own Telethon connection via ``tg_tools``) to inspect folders and
chats, then answers in plain text. The watcher delivers that text to ibo.

Why not Hermes? Hermes's one-shot CLI snapshots its toolset before the Telegram
MCP server finishes connecting, so a single ``hermes -z`` turn never sees the
tools. This loop keeps everything in one warm process and is fully reliable.

Pure stdlib for the HTTP call (urllib), matching ``analyzer.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from datetime import datetime

from watcherdog import (bot_access, daily_report, learned_fixes, self_restart,
                        tg_actions, tg_tools)

logger = logging.getLogger("watcherdog.agent")

# OpenAI-style function schemas for the read tools the model may ALWAYS call.
READ_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_folders",
        "description": "List all Telegram folders (filters) with their ids and titles.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_folder",
        "description": "List the chats inside a folder. Accepts a folder id (int) or "
                       "name (e.g. 'Farms', 'Sam'). Returns each chat's name, id, username.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Folder id or name."}},
            "required": ["folder"]},
    }},
    {"type": "function", "function": {
        "name": "read_chat",
        "description": "Read the most recent messages of a chat. Pass a chat id "
                       "(from get_folder) or an @username. Use this to see what's happening.",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "Chat id or @username."},
            "limit": {"type": "integer", "description": "How many recent messages (1-50, default 15)."}},
            "required": ["chat"]},
    }},
    {"type": "function", "function": {
        "name": "find_chats",
        "description": "Resolve a person/bot/chat by name or @username when you don't "
                       "have its id. Returns matches with name, id, username.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Name or @username to search for."}},
            "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "lookup_fix",
        "description": "Look up a SAVED fix for an error in the learned-fixes brain. "
                       "Always call this first when handling an error. Returns the fix "
                       "(with type ai|human) or null if none is known yet.",
        "parameters": {"type": "object", "properties": {
            "error": {"type": "string", "description": "The error text / message to match."}},
            "required": ["error"]},
    }},
    {"type": "function", "function": {
        "name": "report_progress",
        "description": "Update the user's LIVE progress bar during a multi-step task. "
                       "Call it AS YOU GO — e.g. after each panel/bot you finish — with how "
                       "far along you are (percent 0-100) and a short status note like "
                       "'Panel#2 ✅ Panel#3 ⏳'. Call report_progress(100, 'done') right "
                       "before your final answer. Use it for any task with several steps.",
        "parameters": {"type": "object", "properties": {
            "percent": {"type": "integer", "description": "0-100: how complete the task is."},
            "note": {"type": "string", "description": "Short status line shown next to the bar."}},
            "required": ["percent"]},
    }},
]

# Tools that DRIVE panels or WRITE state — only exposed when actions are enabled.
ACTION_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "panel_menu",
        "description": "Send /start to a panel/bot and return its inline-button labels. "
                       "Use this to see what you can press before acting.",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "Panel chat id or @username."}},
            "required": ["chat"]},
    }},
    {"type": "function", "function": {
        "name": "press_button",
        "description": "Press an inline button on a panel by its label (case-insensitive, "
                       "prefix/substring ok). DESTRUCTIVE buttons (Kill/Restart/Reboot/"
                       "Shutdown) return need_confirm unless confirmed=true — only set "
                       "confirmed=true AFTER ibo says yes. Returns the bot's reply.",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "Panel chat id or @username."},
            "button": {"type": "string", "description": "Button label to press, e.g. 'Start selected accounts'."},
            "confirmed": {"type": "boolean", "description": "Set true ONLY after ibo approved a destructive action."}},
            "required": ["chat", "button"]},
    }},
    {"type": "function", "function": {
        "name": "send_command",
        "description": "Send raw text (e.g. a slash command) to a panel and return its reply.",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "Panel chat id or @username."},
            "text": {"type": "string", "description": "Text/command to send."}},
            "required": ["chat", "text"]},
    }},
    {"type": "function", "function": {
        "name": "screenshot",
        "description": "Press a panel's Screenshot button and download the image. Returns the "
                       "saved path + caption (visual reading needs a vision model).",
        "parameters": {"type": "object", "properties": {
            "chat": {"type": "string", "description": "Panel chat id or @username."}},
            "required": ["chat"]},
    }},
    {"type": "function", "function": {
        "name": "save_fix",
        "description": "Save a new fix to the learned-fixes brain so next time is automatic. "
                       "Call this after ibo teaches you how to handle a new error. "
                       "type='ai' = you fix it next time; type='human' = a person must. "
                       "ALWAYS set 'action' when type='ai' so the fix runs WITHOUT the "
                       "model next time (saves tokens): either 'ignore' for known noise, "
                       "or the exact panel-button labels to press, separated by ' -> ' "
                       "(e.g. 'Kill All CS & Steam -> Start selected accounts'). If any "
                       "step is destructive (Kill/Restart/Reboot/Shutdown), also set "
                       "auto='yes' to allow it to run automatically.",
        "parameters": {"type": "object", "properties": {
            "signature": {"type": "string", "description": "Short name for the error."},
            "match": {"type": "string", "description": "Key phrase that identifies this error."},
            "fix": {"type": "string", "description": "Human-readable steps (or 'wait for human')."},
            "type": {"type": "string", "description": "'ai' or 'human'."},
            "action": {"type": "string", "description": "Machine-executable steps: 'ignore', "
                       "or button labels separated by ' -> '. Blank = ask the model next time."},
            "auto": {"type": "string", "description": "'yes' to let a destructive action run automatically."},
            "notes": {"type": "string", "description": "Optional gotchas."}},
            "required": ["signature", "match", "fix"]},
    }},
    {"type": "function", "function": {
        "name": "log_fix",
        "description": "Record an error you fixed YOURSELF, for the end-of-day report. "
                       "Call this after you apply a fix automatically.",
        "parameters": {"type": "object", "properties": {
            "panel": {"type": "string", "description": "Panel/bot name, e.g. 'SinFermera23'."},
            "error": {"type": "string", "description": "Short error description."},
            "fix": {"type": "string", "description": "What you did."},
            "result": {"type": "string", "description": "Outcome, e.g. 'ok'."}},
            "required": ["panel", "error", "fix"]},
    }},
]


# Access-management tools — only exposed when the REQUESTER is an admin
# (``can_grant``). They let the owner say "give @user access to use the bot" and
# have the agent grant it itself, persisted via watcherdog.bot_access.
GRANT_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "grant_bot_access",
        "description": "Grant a user permission to USE the bot (drive panels: stop, "
                       "press buttons, send commands). Call this when an owner tells you "
                       "to give someone access. Accepts an @username or numeric user id.",
        "parameters": {"type": "object", "properties": {
            "user": {"type": "string", "description": "@username or numeric id to grant access to."}},
            "required": ["user"]},
    }},
    {"type": "function", "function": {
        "name": "revoke_bot_access",
        "description": "Remove a user's permission to use the bot. Accepts an @username "
                       "or numeric user id.",
        "parameters": {"type": "object", "properties": {
            "user": {"type": "string", "description": "@username or numeric id to revoke."}},
            "required": ["user"]},
    }},
    {"type": "function", "function": {
        "name": "list_bot_access",
        "description": "List the users who currently have granted access to use the bot.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


# Self-editing tools — let the agent read and modify WatcherDog's OWN project
# files on an owner's command. Only exposed to admins when BOT_SELF_EDIT_ENABLED.
# Every write makes a timestamped backup; paths can't escape the project root.
EDIT_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "apply_code_change",
        "description": "PREFERRED way to change WatcherDog's own code: describe the change "
                       "in plain language and a careful code-editor reads the whole file, "
                       "applies it, and syntax-checks the result before saving (a backup is "
                       "kept; a broken result is refused, never written). Use this instead of "
                       "edit_project_file for anything non-trivial — e.g. \"add a /uptime "
                       "command to commands.py that reports how long the bot has run\".",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File to change, relative to the project root."},
            "instruction": {"type": "string", "description": "Plain-language description of the change to make."}},
            "required": ["path", "instruction"]},
    }},
    {"type": "function", "function": {
        "name": "update_setting",
        "description": "Change a WatcherDog SETTING reliably (a .env value) — use this for "
                       "behaviour tweaks like thresholds/intervals INSTEAD of editing code. "
                       "E.g. 'quiet at 1 hour' → update_setting('QUIET_THRESHOLD_MINUTES', "
                       "'60'). Only known settings are allowed. Then call restart_watcher to "
                       "apply it.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Setting name, e.g. QUIET_THRESHOLD_MINUTES."},
            "value": {"type": "string", "description": "New value, e.g. '60'."}},
            "required": ["key", "value"]},
    }},
    {"type": "function", "function": {
        "name": "list_project_files",
        "description": "List files/dirs inside the WatcherDog project (relative paths). "
                       "Use this to find the file you need before reading or editing.",
        "parameters": {"type": "object", "properties": {
            "dir": {"type": "string", "description": "Subdirectory, relative to the project "
                    "root (blank = root)."}}},
    }},
    {"type": "function", "function": {
        "name": "read_project_file",
        "description": "Read a WatcherDog source file (relative path from the project root). "
                       "Always read a file before editing it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."}},
            "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "edit_project_file",
        "description": "Replace an EXACT substring in a WatcherDog file with new text (a "
                       "backup is saved first). Prefer this for small changes. 'old' must "
                       "match exactly and appear once.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."},
            "old": {"type": "string", "description": "Exact text to replace (must be unique)."},
            "new": {"type": "string", "description": "Replacement text."}},
            "required": ["path", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "write_project_file",
        "description": "Create or OVERWRITE a WatcherDog file with the given content (a "
                       "backup of any existing file is saved first). Use for new files or "
                       "full rewrites; prefer edit_project_file for small changes.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the project root."},
            "content": {"type": "string", "description": "Full new file content."}},
            "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "restart_watcher",
        "description": "Restart WatcherDog so the code changes you just made take effect. "
                       "SAFE: it first checks the whole project still imports — if not, it "
                       "rolls back your change and does NOT restart (the bot keeps running). "
                       "After restarting, a supervisor auto-rolls-back and relaunches if the "
                       "new code fails to come up. Call this LAST, once your edits are done.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


# Fan-out tool — do the same action on many bots in PARALLEL (sub-agents), with a
# deterministic X/N progress bar and one combined report. Action-capable; only
# offered on the top-level turn (sub-agents get allow_fanout=False) to avoid
# recursion.
DISPATCH_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "dispatch_bots",
        "description": "Do the SAME action on MANY bots AT ONCE (in parallel) — far faster "
                       "than one at a time. Each bot is handled by its own sub-agent "
                       "concurrently; a live 'X/N done' progress bar is shown and you get "
                       "ONE combined report back. Use this whenever the task spans several / "
                       "all farm bots (e.g. 'fix all accounts', 'restart everything', "
                       "'screenshot all panels'). Plan first, then call this once.",
        "parameters": {"type": "object", "properties": {
            "targets": {"type": "string", "description": "'all' / 'farms' for the whole Farms "
                        "folder, or a comma-separated list of SinFermera numbers, ids, or "
                        "@usernames (e.g. '3,5,12')."},
            "instruction": {"type": "string", "description": "What to do on EACH bot, in plain "
                            "language (e.g. 'kill stuck CS & Steam, then launch 4 accounts')."}},
            "required": ["targets", "instruction"]},
    }},
]


def build_tools(cfg, *, can_grant=False, can_edit=False, allow_fanout=True):
    """The tool list for one turn: read + lookup always; action/write tools only
    when AGENT_ACTIONS_ENABLED; access-management tools only for admins
    (``can_grant``); self-editing tools only for admins when the capability is on
    (``can_edit``); the parallel fan-out tool only on the top-level turn
    (``allow_fanout``)."""
    tools = list(READ_TOOL_SCHEMAS)
    if getattr(cfg, "agent_actions_enabled", False):
        tools += ACTION_TOOL_SCHEMAS
        if allow_fanout:
            tools += DISPATCH_TOOL_SCHEMAS
    if can_grant:
        tools += GRANT_TOOL_SCHEMAS
    if can_edit:
        tools += EDIT_TOOL_SCHEMAS
    return tools


# --- self-editing helpers ---------------------------------------------------
def _safe_project_path(cfg, rel):
    """Resolve `rel` against the project root, refusing anything that escapes it.

    Returns the absolute path, or None if `rel` is empty / outside the root.
    Self-editing is confined to the WatcherDog project directory."""
    root = os.path.realpath(cfg.root)
    rel = (rel or "").strip()
    if not rel:
        return None
    candidate = os.path.realpath(os.path.join(root, rel))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def _backup_file(path):
    """Copy `path` to `path.bak.<unixtime>` before it's modified. Returns the
    backup path, or "" if there was nothing to back up."""
    if not os.path.exists(path):
        return ""
    bak = f"{path}.bak.{int(time.time())}"
    with open(path, "rb") as src, open(bak, "wb") as dst:
        dst.write(src.read())
    return bak


def _python_syntax_error(rel, content):
    """If `rel` is a .py file whose `content` isn't valid Python, return a short
    error string; else None. Guards self-edits from leaving the agent with code
    that won't import (which would crash the watcher on restart)."""
    if not str(rel).endswith(".py"):
        return None
    try:
        compile(content, str(rel), "exec")
        return None
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"


def _list_project_files(cfg, rel):
    target = _safe_project_path(cfg, rel or ".") if rel else os.path.realpath(cfg.root)
    if target is None or not os.path.isdir(target):
        return {"error": f"not a directory inside the project: {rel!r}"}
    root = os.path.realpath(cfg.root)
    entries = []
    for name in sorted(os.listdir(target)):
        if name.startswith(".") and name not in (".env.example",):
            continue  # skip dotfiles/dirs (incl. .env, .git, .venv) from listings
        full = os.path.join(target, name)
        entries.append({"path": os.path.relpath(full, root),
                        "type": "dir" if os.path.isdir(full) else "file"})
    return {"dir": os.path.relpath(target, root), "entries": entries}


def _read_project_file(cfg, rel):
    path = _safe_project_path(cfg, rel)
    if path is None:
        return {"error": f"path is empty or escapes the project root: {rel!r}"}
    if not os.path.isfile(path):
        return {"error": f"no such file: {rel!r}"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"could not read {rel!r}: {exc}"}
    return {"path": rel, "content": content[:60000],
            "truncated": len(content) > 60000}


def _write_project_file(cfg, rel, content):
    path = _safe_project_path(cfg, rel)
    if path is None:
        return {"error": f"path is empty or escapes the project root: {rel!r}"}
    syntax_err = _python_syntax_error(rel, content)
    if syntax_err is not None:
        return {"error": f"REFUSED — that content has a Python syntax error ({syntax_err}). "
                         f"The file was NOT changed. Fix the content and try again.",
                "syntax_error": syntax_err}
    bak = _backup_file(path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return {"error": f"could not write {rel!r}: {exc}"}
    self_restart.record_edit(cfg, path, bak)
    logger.info("self-edit: wrote %s (%d bytes, backup=%s)", rel, len(content), bak or "none")
    return {"written": rel, "bytes": len(content), "backup": os.path.basename(bak) if bak else None,
            "note": "Call restart_watcher to apply this (or restart manually)."}


def _edit_project_file(cfg, rel, old, new):
    path = _safe_project_path(cfg, rel)
    if path is None:
        return {"error": f"path is empty or escapes the project root: {rel!r}"}
    if not os.path.isfile(path):
        return {"error": f"no such file: {rel!r}"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"could not read {rel!r}: {exc}"}
    if not old:
        return {"error": "'old' is empty — use write_project_file to overwrite"}
    count = text.count(old)
    if count == 0:
        return {"error": "'old' text not found in the file"}
    if count > 1:
        return {"error": f"'old' text appears {count} times — make it unique"}
    updated = text.replace(old, new, 1)
    syntax_err = _python_syntax_error(rel, updated)
    if syntax_err is not None:
        return {"error": f"REFUSED — that edit would create a Python syntax error "
                         f"({syntax_err}). The file was NOT changed. Re-read the file "
                         f"and adjust your 'old'/'new' so the result stays valid.",
                "syntax_error": syntax_err}
    bak = _backup_file(path)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except OSError as exc:
        return {"error": f"could not write {rel!r}: {exc}"}
    self_restart.record_edit(cfg, path, bak)
    logger.info("self-edit: edited %s (backup=%s)", rel, bak or "none")
    return {"edited": rel, "backup": os.path.basename(bak) if bak else None,
            "note": "Call restart_watcher to apply this (or restart manually)."}


# Settings an admin may change through the bot (safe, tunable knobs only — no
# tokens, paths, or capability flags that could lock the bot out of itself).
_ALLOWED_SETTINGS = {
    "QUIET_THRESHOLD_MINUTES", "SILENCE_THRESHOLD_MINUTES",
    "SILENCE_CHECK_INTERVAL_SECONDS", "WATCH_POLL_INTERVAL", "MIN_SEVERITY",
    "DEDUPE_WINDOW", "AGENT_MAX_STEPS", "BOT_MAX_CONCURRENT", "FANOUT_CONCURRENCY",
    "STICKER_CHANCE", "HOURLY_REPORT_ENABLED", "HOURLY_REPORT_TOPIC",
}


def _update_setting(cfg, key, value):
    """Set a known WatcherDog setting in .env (reliable, no code editing). Returns
    a result dict; the change applies on the next restart."""
    key = (key or "").strip().upper()
    if key not in _ALLOWED_SETTINGS:
        return {"error": f"{key!r} is not a changeable setting. Allowed: "
                         f"{', '.join(sorted(_ALLOWED_SETTINGS))}"}
    value = str(value).strip()
    env_path = os.path.join(cfg.root, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return {"error": f"could not read .env: {exc}"}
    bak = _backup_file(env_path)
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    try:
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError as exc:
        return {"error": f"could not write .env: {exc}"}
    self_restart.record_edit(cfg, env_path, bak)
    logger.info("update_setting: %s=%s (backup=%s)", key, value, bak or "none")
    return {"updated": key, "value": value,
            "backup": os.path.basename(bak) if bak else None,
            "note": "Call restart_watcher to apply this."}


def _strip_code_fence(text):
    """Remove a surrounding ```lang ... ``` markdown fence if the model added one."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t


_CODE_EDITOR_SYSTEM = (
    "You are a precise code editor. You receive the FULL contents of one file and "
    "an instruction. Output the COMPLETE new file content with the instruction "
    "applied — the whole file, start to finish, nothing else: no explanations, no "
    "commentary, no markdown fences. Preserve every line not covered by the "
    "instruction byte-for-byte. Keep the language and style consistent. If the "
    "instruction is unclear or unsafe, output the file UNCHANGED."
)


async def _apply_code_change(cfg, rel, instruction):
    """Plan-then-apply self-edit: read the WHOLE file, have the model rewrite it
    in one focused pass with the instruction applied, syntax-check the result, and
    write only if valid (with a backup). Avoids fragile old/new string matching.

    The result is fail-closed: a truncated or broken rewrite is rejected, never
    written, so the agent can't corrupt its own code."""
    path = _safe_project_path(cfg, rel)
    if path is None:
        return {"error": f"path is empty or escapes the project root: {rel!r}"}
    if not os.path.isfile(path):
        return {"error": f"no such file: {rel!r} (use write_project_file for a new file)"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"could not read {rel!r}: {exc}"}
    if len(original) > 100000:
        return {"error": f"{rel} is too large to rewrite whole — use edit_project_file "
                         "for a targeted change."}
    if not (instruction or "").strip():
        return {"error": "instruction is empty — say what to change"}

    messages = [
        {"role": "system", "content": _CODE_EDITOR_SYSTEM},
        {"role": "user", "content": f"FILE: {rel}\nINSTRUCTION: {instruction}\n\n"
                                    f"--- CURRENT CONTENT ---\n{original}"},
    ]
    loop = asyncio.get_running_loop()
    msg = await loop.run_in_executor(None, _chat_completion, cfg, messages, None)
    new = _strip_code_fence((msg or {}).get("content") or "")
    if not new.strip():
        return {"error": "the code editor returned nothing — try rephrasing the instruction"}
    syntax_err = _python_syntax_error(rel, new)
    if syntax_err is not None:
        return {"error": f"REFUSED — the rewrite has a syntax error ({syntax_err}); the file "
                         "was NOT changed. (Often a truncated rewrite — try a smaller, more "
                         "specific instruction, or edit_project_file.)",
                "syntax_error": syntax_err}
    if new == original:
        return {"unchanged": rel, "note": "the editor produced no change"}
    bak = _backup_file(path)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    except OSError as exc:
        return {"error": f"could not write {rel!r}: {exc}"}
    self_restart.record_edit(cfg, path, bak)
    logger.info("self-edit: apply_code_change %s (%d→%d lines, backup=%s)",
                rel, original.count("\n") + 1, new.count("\n") + 1, bak or "none")
    return {"applied": rel, "backup": os.path.basename(bak) if bak else None,
            "lines_before": original.count("\n") + 1, "lines_after": new.count("\n") + 1,
            "note": "Call restart_watcher to apply this (or restart manually)."}


async def _resolve_targets(cfg, client, targets):
    """Resolve a `targets` string to a list of (name, chat_id) bots.

    'all'/'farms'/'' → the whole watch folder. Otherwise a comma list of
    SinFermera numbers (small ints), chat ids (large ints), or @usernames —
    matched against the folder roster first, then resolved directly."""
    from telethon.utils import get_peer_id
    t = (targets or "").strip().lower()
    try:
        roster = (await tg_tools.folder_chats(client, cfg.watch_folder)).get("chats", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("dispatch: could not read folder %r: %s", cfg.watch_folder, exc)
        roster = []
    if t in ("", "all", "farms", "everything", "*"):
        return [(c["name"], c["id"]) for c in roster]

    out, seen = [], set()
    for tok in str(targets).split(","):
        tok = tok.strip()
        if not tok:
            continue
        match = None
        if tok.isdigit() and int(tok) <= 100:           # a SinFermera number
            for c in roster:
                nm = (c.get("name") or "").rstrip()
                if nm == f"SinFermera{tok}" or nm.endswith(tok):
                    match = c
                    break
        if match is None:                                # name / @username match
            for c in roster:
                nm = (c.get("name") or "").lower()
                un = (c.get("username") or "").lower()
                if tok.lower() in nm or tok.lstrip("@").lower() == un:
                    match = c
                    break
        if match is not None:
            cid, name = match["id"], match["name"]
        else:                                            # resolve directly (id/@username)
            try:
                ent = await client.get_entity(int(tok) if tok.lstrip("-").isdigit() else tok)
                cid, name = get_peer_id(ent), tg_tools.entity_name(ent)
            except Exception:  # noqa: BLE001
                logger.warning("dispatch: could not resolve target %r", tok)
                continue
        if cid not in seen:
            seen.add(cid)
            out.append((name, cid))
    return out


async def _dispatch_bots(cfg, client, targets, instruction, *, execute, on_progress,
                         system_prompt):
    """Run `instruction` on every resolved bot IN PARALLEL, each via its own
    sub-agent, with a deterministic X/N progress bar and aggregated results.

    Concurrency is bounded by FANOUT_CONCURRENCY; a per-bot lock serializes work
    on the SAME bot while different bots run simultaneously."""
    bots = await _resolve_targets(cfg, client, targets)
    if not bots:
        return {"error": f"no bots matched {targets!r}"}
    total = len(bots)
    state = {"done": 0, "results": []}
    locks, sem = {}, asyncio.Semaphore(max(1, getattr(cfg, "fanout_concurrency", 4)))

    async def _bar(extra=""):
        if on_progress is None:
            return
        pct = round(state["done"] / total * 100)
        bar = "▰" * round(pct / 10) + "▱" * (10 - round(pct / 10))
        await on_progress("dispatch_bots", f"{state['done']}/{total} bots  {bar} {pct}%"
                          + (f"  ·  {extra}" if extra else ""))

    await _bar("starting…")

    async def _one(name, cid):
        sub_prompt = (
            f"You are ONE worker in a parallel fan-out, assigned EXACTLY ONE bot: {name} "
            f"(chat id {cid}). Do this on THIS bot only, using your panel tools: "
            f"{instruction}. Do NOT touch any other bot, and do NOT try to dispatch or fan "
            "out further — just operate on your one bot directly. When finished, reply with "
            "ONE short line: the bot name and the outcome.\n\n" + (system_prompt or ""))
        lock = locks.setdefault(cid, asyncio.Lock())
        async with sem:
            try:
                ans, _ = await answer(
                    cfg, client, f"{instruction}  (operate only on {name}, chat {cid})",
                    system_prompt=sub_prompt, execute=execute,
                    action_lock=lock, allow_fanout=False)
            except Exception as exc:  # noqa: BLE001
                ans = f"error: {type(exc).__name__}: {exc}"
        state["done"] += 1
        state["results"].append({"bot": name, "outcome": (ans or "").strip()[:200]})
        await _bar(name)

    await asyncio.gather(*[_one(n, c) for n, c in bots])
    return {"total": total, "results": state["results"]}


async def _resolve_user(client, ref):
    """Resolve a user ref (@username or numeric id) to (user_id, label). Returns
    (None, "") if it can't be resolved."""
    ref = (ref or "").strip()
    if not ref:
        return None, ""
    if ref.lstrip("-").isdigit():
        return int(ref), ref
    try:
        ent = await client.get_entity(ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("grant: could not resolve user %r: %s", ref, exc)
        return None, ""
    from telethon.utils import get_peer_id
    return get_peer_id(ent), tg_tools.entity_name(ent)


async def _dispatch(client, name, args, cfg=None, *, execute=True, can_grant=False,
                    can_edit=False, on_progress=None, system_prompt=""):
    """Execute one tool call against the watcher's Telethon client. Returns a
    JSON-serializable result (or an {'error': ...} dict — never raises)."""
    mark_read = bool(cfg and getattr(cfg, "mark_read_after_read", False))
    actions_on = bool(cfg and getattr(cfg, "agent_actions_enabled", False))
    # Tools that drive panels or write state are gated twice: the capability must
    # be enabled, and `execute` must be on (off during a dry run — the model is
    # told it would act but nothing real happens).
    _ACTION = {"panel_menu", "press_button", "send_command", "screenshot",
               "save_fix", "log_fix", "dispatch_bots"}
    if name in _ACTION:
        if not actions_on:
            return {"error": "actions are disabled (AGENT_ACTIONS_ENABLED=false) — read-only"}
        if not execute:
            return {"error": "dry-run: would act but execution is disabled"}
    # Access-management tools require the requester to be an admin.
    _GRANT = {"grant_bot_access", "revoke_bot_access", "list_bot_access"}
    if name in _GRANT and not can_grant:
        return {"error": "not authorized: only an admin can manage bot access"}
    # Self-editing tools require admin + the self-edit capability; writes also
    # require execute (off in a dry run).
    _EDIT = {"list_project_files", "read_project_file", "write_project_file",
             "edit_project_file", "apply_code_change", "restart_watcher",
             "update_setting"}
    _EDIT_WRITE = {"write_project_file", "edit_project_file", "apply_code_change",
                   "restart_watcher", "update_setting"}
    if name in _EDIT:
        if not can_edit:
            return {"error": "not authorized: self-editing is disabled or you are not an admin"}
        if name in _EDIT_WRITE and not execute:
            return {"error": "dry-run: would edit files but execution is disabled"}
    try:
        if name == "report_progress":
            return {"ok": True}  # UI-only; the bar is rendered from the call args
        if name == "list_folders":
            return {"folders": await tg_tools.list_folders(client)}
        if name == "get_folder":
            return await tg_tools.folder_chats(client, _coerce_folder(args.get("folder")))
        if name == "read_chat":
            return await tg_tools.read_history(client, args.get("chat"), args.get("limit", 15),
                                               mark_read=mark_read)
        if name == "find_chats":
            return {"matches": await tg_tools.find_chats(client, args.get("query", ""))}
        if name == "lookup_fix":
            path = cfg.learned_fixes_path if cfg else None
            fix = learned_fixes.find_fix(args.get("error", ""), path=path)
            return {"fix": fix}
        if name == "panel_menu":
            return await tg_actions.panel_menu(client, args.get("chat"))
        if name == "press_button":
            return await tg_actions.press_button(
                client, args.get("chat"), args.get("button", ""),
                confirmed=bool(args.get("confirmed", False)))
        if name == "send_command":
            return await tg_actions.send_command(client, args.get("chat"), args.get("text", ""))
        if name == "screenshot":
            return await tg_actions.screenshot(client, args.get("chat"), cfg=cfg)
        if name == "dispatch_bots":
            return await _dispatch_bots(
                cfg, client, args.get("targets", ""), args.get("instruction", ""),
                execute=execute, on_progress=on_progress, system_prompt=system_prompt)
        if name == "save_fix":
            saved = learned_fixes.append_fix(
                cfg.learned_fixes_path, signature=args.get("signature", "error"),
                match=args.get("match", ""), fix=args.get("fix", ""),
                type=args.get("type", "ai"), notes=args.get("notes", ""),
                action=args.get("action", ""), auto=args.get("auto", ""),
                date=datetime.now().date().isoformat())
            return {"saved": saved}
        if name == "log_fix":
            entry = daily_report.record(
                cfg.daily_errors_path, panel=args.get("panel", "?"),
                error=args.get("error", ""), fix=args.get("fix", ""),
                result=args.get("result", "ok"))
            return {"logged": entry}
        if name == "grant_bot_access":
            uid, label = await _resolve_user(client, args.get("user", ""))
            if uid is None:
                return {"error": "could not resolve that user — give an @username "
                                 "or a numeric id"}
            added = bot_access.grant(cfg.bot_access_path, uid, label)
            return {"granted": {"id": uid, "label": label},
                    "already_had_access": not added}
        if name == "revoke_bot_access":
            uid, label = await _resolve_user(client, args.get("user", ""))
            if uid is None:
                return {"error": "could not resolve that user — give an @username "
                                 "or a numeric id"}
            removed = bot_access.revoke(cfg.bot_access_path, uid)
            return {"revoked": {"id": uid, "label": label}, "had_access": removed}
        if name == "list_bot_access":
            return {"granted_users": bot_access.list_users(cfg.bot_access_path)}
        if name == "list_project_files":
            return _list_project_files(cfg, args.get("dir", ""))
        if name == "read_project_file":
            return _read_project_file(cfg, args.get("path", ""))
        if name == "write_project_file":
            return _write_project_file(cfg, args.get("path", ""), args.get("content", ""))
        if name == "edit_project_file":
            return _edit_project_file(cfg, args.get("path", ""),
                                      args.get("old", ""), args.get("new", ""))
        if name == "apply_code_change":
            return await _apply_code_change(cfg, args.get("path", ""),
                                            args.get("instruction", ""))
        if name == "update_setting":
            return _update_setting(cfg, args.get("key", ""), args.get("value", ""))
        if name == "restart_watcher":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self_restart.request_restart, cfg)
        return {"error": f"unknown tool {name!r}"}
    except KeyError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool %s failed: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


# Panel-driving tools that must not run concurrently across turns (they share the
# one user account). The agent grabs the action lock the moment it first calls one
# of these and holds it until the turn ends, so a whole action sequence is atomic
# while read-only turns never block. save_fix/log_fix write files (not panels), so
# they aren't here.
PANEL_TOOLS = {"panel_menu", "press_button", "send_command", "screenshot"}


def _tool_label(name, args):
    """A short, friendly, emoji-prefixed description of a tool call, for the live
    progress status (e.g. "🔘 pressing 'Stop' on SinFermera3")."""
    a = args or {}
    short = lambda v, n=40: str(v)[:n]  # noqa: E731
    if name == "report_progress":
        try:
            pct = max(0, min(100, int(a.get("percent", 0))))
        except (TypeError, ValueError):
            pct = 0
        bar = "▰" * round(pct / 10) + "▱" * (10 - round(pct / 10))
        note = short(a.get("note", ""), 90)
        return f"{bar} {pct}%" + (f"  ·  {note}" if note else "")
    labels = {
        "list_folders": "🗂 listing folders",
        "get_folder": f"📂 opening folder {short(a.get('folder', ''))}",
        "read_chat": f"📖 reading {short(a.get('chat', ''))}",
        "find_chats": f"🔍 looking up {short(a.get('query', ''))}",
        "lookup_fix": "🧠 checking saved fixes",
        "panel_menu": f"🎛 opening {short(a.get('chat', ''))} menu",
        "press_button": f"🔘 pressing '{short(a.get('button', ''))}' on {short(a.get('chat', ''))}",
        "send_command": f"⌨️ sending to {short(a.get('chat', ''))}",
        "screenshot": f"📸 screenshotting {short(a.get('chat', ''))}",
        "dispatch_bots": f"🚀 fanning out to bots: {short(a.get('targets', ''))}",
        "save_fix": "💾 saving a fix",
        "log_fix": f"📝 logging fix for {short(a.get('panel', ''))}",
        "grant_bot_access": f"🔑 granting access to {short(a.get('user', ''))}",
        "revoke_bot_access": f"🔒 revoking access from {short(a.get('user', ''))}",
        "list_bot_access": "👥 listing access",
        "list_project_files": f"📁 listing files {short(a.get('dir', ''))}",
        "read_project_file": f"📄 reading file {short(a.get('path', ''))}",
        "update_setting": f"⚙️ setting {short(a.get('key', ''))}={short(a.get('value', ''), 20)}",
        "apply_code_change": f"🛠 rewriting {short(a.get('path', ''))}",
        "restart_watcher": "🔄 validating & restarting",
        "edit_project_file": f"✏️ editing {short(a.get('path', ''))}",
        "write_project_file": f"🖊 writing {short(a.get('path', ''))}",
    }
    return labels.get(name, f"⚙️ running {name}")


def _coerce_folder(val):
    """A folder arg that is all digits is an id; otherwise a name."""
    if isinstance(val, str) and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return val


def _chat_completion(cfg, messages, tools):
    """Blocking POST to the OpenAI-compatible chat endpoint. Returns the parsed
    `message` dict of choice 0, or None on failure.

    Pass ``tools=None`` (or empty) to omit the tool schemas entirely — the model
    then can't call a tool and must answer in plain text. Used for the final
    forced answer once the step budget is spent."""
    payload = {
        "model": cfg.agent_model,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{cfg.agent_api_base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.agent_api_key}",
            "HTTP-Referer": "https://github.com/watcherdog",
            "X-Title": "WatcherDog",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.agent_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
        logger.warning("chat API call failed: %s %s", exc, detail)
        return None
    try:
        return body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        logger.warning("chat API returned no choices: %s", str(body)[:300])
        return None


def _last_assistant_text(messages):
    """Most recent non-empty assistant `content` in the running transcript, used
    as a last-resort answer if the forced final pass also comes back empty."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            text = (m.get("content") or "").strip()
            if text:
                return text
    return ""


async def answer(cfg, client, user_text, *, system_prompt, history=None, execute=True,
                 can_grant=False, can_edit=False, on_progress=None, action_lock=None,
                 allow_fanout=True):
    """Run the tool-calling loop for one ibo message.

    Returns (answer_text, new_history). `history` is a list of prior
    {role, content} turns (user/assistant only); the returned new_history appends
    this exchange, trimmed to cfg.agent_history_turns pairs. When `execute` is
    False (a dry run) the panel/write tools are advertised but refuse to act.
    `can_grant`/`can_edit` (admin-only) expose the access-management and
    self-editing tools respectively. `on_progress`, if given, is an async callable
    invoked as ``on_progress(tool_name, label)`` before each tool runs (live
    status).

    `action_lock`, if given, is acquired LAZILY — only the moment the agent first
    drives a panel — and held until the turn ends, so a whole action sequence is
    atomic across turns while read-only turns never block on it.
    """
    async def _progress(name, label):
        if on_progress is None:
            return
        try:
            await on_progress(name, label)
        except Exception:  # noqa: BLE001
            logger.debug("progress callback failed for %r", label)

    history = history or []
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": user_text})

    tools = build_tools(cfg, can_grant=can_grant, can_edit=can_edit, allow_fanout=allow_fanout)
    loop = asyncio.get_running_loop()
    final = None
    held_lock = False  # whether we've acquired action_lock for this turn
    try:
        for step in range(cfg.agent_max_steps):
            msg = await loop.run_in_executor(None, _chat_completion, cfg, messages, tools)
            if msg is None:
                final = "⚠️ WatcherDog: the assistant model is unreachable right now."
                break
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final = (msg.get("content") or "").strip() or "(no answer)"
                break
            # Record the assistant turn (with its tool calls) verbatim, then run each.
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                logger.info("agent tool: %s(%s)", name, args)
                # First real panel action this turn → grab the shared action lock
                # (only when execution is on, so read-only turns never take it).
                if (name in PANEL_TOOLS and execute and action_lock is not None
                        and not held_lock):
                    if action_lock.locked():
                        await _progress(name, "⏳ waiting for panel access…")
                    await action_lock.acquire()
                    held_lock = True
                await _progress(name, _tool_label(name, args))
                result = await _dispatch(client, name, args, cfg, execute=execute,
                                         can_grant=can_grant, can_edit=can_edit,
                                         on_progress=_progress, system_prompt=system_prompt)
                # File reads need the WHOLE file so self-edits aren't done blind;
                # other tool results stay tightly capped to save tokens.
                cap = 60000 if name == "read_project_file" else 6000
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": json.dumps(result)[:cap],
                })
        else:
            # Step budget spent without a text answer. Rather than surfacing a
            # "couldn't finish" error to ibo, make one final pass with NO tools so
            # the model is forced to answer from what it already gathered.
            msg = await loop.run_in_executor(None, _chat_completion, cfg, messages, None)
            final = ((msg or {}).get("content") or "").strip()
            if not final:
                final = _last_assistant_text(messages) or "(no answer)"
    finally:
        if held_lock and action_lock is not None:
            action_lock.release()

    new_history = history + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": final},
    ]
    max_msgs = max(2, cfg.agent_history_turns * 2)
    return final, new_history[-max_msgs:]
