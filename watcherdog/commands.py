"""Prefix slash-commands for the ibo chat — one-tap farm queries.

A short command ibo types (``/weekly``, ``/today``, ``/check 5`` …) is expanded
into a rich, structured prompt for the conversation agent (``agent.answer``), so
answers come back consistently formatted instead of depending on how the question
was phrased. The same expansion powers the scheduled weekly digest.

Meta commands (``/help`` / ``/commands``) are answered **directly** by
:func:`static_reply` — they don't go to the model.

This module is pure string work — no Telegram and no model calls — so it's easy
to unit-test and cheap to extend: add a builder + a row in :data:`MENU`.
"""

from __future__ import annotations

import re

from watcherdog import task_store

# Default Farms folder id (see docs/hermes/STRUCTURE.md). The agent re-checks via
# its tools; this just points it at the right place.
_FARMS = "the Farms folder (id 6) — the SinFermera bots"

_SKIMMABLE = "Keep it short and skimmable — it's read on a phone."


def _botname(token):
    """A bare number means SinFermera<n>; anything else is used verbatim."""
    token = token.strip()
    return f"SinFermera{token}" if token.isdigit() else token


# --- 💰 money / output ------------------------------------------------------
def _weekly(args, cfg):
    return (
        f"Compile the WEEKLY farm report. Read {_FARMS} (each bot's recent "
        "messages) and report:\n"
        "• Headline: total drops collected this week and an estimated $ value "
        "(parse the price tails the bots post).\n"
        "• Per-bot breakdown: drops and value.\n"
        "• Top 3 and bottom 3 performers.\n"
        "• Week-over-week change if you can infer it.\n"
        f"{_SKIMMABLE} Lead with the one-line headline (total drops · ~$value)."
    )


def _today(args, cfg):
    return (
        f"Give TODAY's farm summary. Read {_FARMS} and report today's drops, an "
        "estimated $ value, and any notable events (bans, restarts, stuck "
        f"matches, silences). {_SKIMMABLE} One-line headline, then only the "
        "bullets that matter."
    )


def _top(args, cfg):
    return (
        f"List the TOP-performing bots this week by drops (and $ value if you can "
        f"read it). Read {_FARMS}. Show the best 5 — bot number, drops, value. "
        f"One line each, highest first. {_SKIMMABLE}"
    )


def _worst(args, cfg):
    return (
        f"List the WORST / laggard bots this week — lowest drops or stalled. Read "
        f"{_FARMS}. Show the bottom 5 — bot number, drops, and why it's low if "
        f"that's visible (stuck, silent, banned). {_SKIMMABLE}"
    )


def _value(args, cfg):
    return (
        f"Estimate the total $ VALUE of everything collected (parse the price "
        f"tails the bots post). Read {_FARMS}. Give the grand total, then the top "
        f"contributing bots. {_SKIMMABLE}"
    )


# --- 🩺 health / triage -----------------------------------------------------
# NOTE: /status, /problems, /silent are now answered DETERMINISTICALLY (no model)
# by watcherdog/fast_commands.py off the shared roster scan — see FAST_MENU below.
def _check(args, cfg):
    args = (args or "").strip()
    if not args:
        return ("ibo wants a deep-dive on a specific bot but didn't say which. "
                "Ask which SinFermera bot (number or name) to check.")
    bot = _botname(args)
    return (
        f"Deep-dive on {bot}. Read its recent messages in {_FARMS} and report: "
        "what it's doing right now, drops/accounts/warmups, and any problems "
        f"(errors, bans, stalls). Be specific and cite what it actually posted. "
        f"{_SKIMMABLE}"
    )


def _bans(args, cfg):
    return (
        f"Scan {_FARMS} for BANNED or suspended accounts and any captcha / Steam "
        "Guard prompts — these are high priority. For each, say which bot, which "
        "account, and what the prompt or ban message says. If there are none, "
        "reply 'no bans or Steam Guard / captcha prompts right now'."
    )


# --- 📊 compare / trends ----------------------------------------------------
def _compare(args, cfg):
    toks = (args or "").split()
    if len(toks) < 2:
        return ("ibo wants to compare two bots but didn't give two. Ask which "
                "two SinFermera bots (numbers or names) to compare.")
    a, b = _botname(toks[0]), _botname(toks[1])
    return (
        f"Compare {a} vs {b} side by side. Read each one's recent messages in "
        f"{_FARMS} and contrast drops, accounts launched, warmups, and any "
        f"problems. Two short sections, one per bot. {_SKIMMABLE}"
    )


# --- 🛠 self-improve ---------------------------------------------------------
def _improve(args, cfg):
    what = (args or "").strip()
    if not what:
        return ("The owner wants you to improve your own code/behaviour but "
                "didn't say what. Ask, in one line, what to change.")
    return (
        "The owner is asking you to CHANGE WATCHERDOG ITSELF (your own code / "
        "settings / skills). Follow skill 7 (self-improve): investigate the right "
        "file with list_project_files / read_project_file, make the smallest "
        "change with update_setting (for a threshold/flag) or edit_project_file / "
        "apply_code_change (for code), then restart_watcher to deploy — it "
        "validates and rolls back if broken. This is admin-gated: if you lack the "
        "self-edit capability this turn, say so in one line and stop. Report what "
        "you changed in past tense.\n\n"
        f"Requested change:\n{what}"
    )


# --- 🗂 other ---------------------------------------------------------------
def _whatsnew(args, cfg):
    return (
        "Summarize anything NEW / unread across the account. Use the Unread "
        "folder (id 4) and the other folders. Group by folder/chat, newest first. "
        f"{_SKIMMABLE}"
    )


# The menu: (syntax, builder, help-text), grouped. Order here is the order shown
# by /help, and it's the single source of truth for COMMANDS + aliases.
MENU = [
    ("💰 Money / output", [
        ("/weekly", _weekly, "weekly report: drops, est $, per-bot, top/bottom 3"),
        ("/today", _today, "today's drops, value, notable events"),
        ("/top", _top, "best-performing bots this week"),
        ("/worst", _worst, "laggard / stalled bots this week"),
        ("/value", _value, "estimated $ value of everything collected"),
    ]),
    ("🩺 Health / triage", [
        ("/check <n>", _check, "deep-dive on one bot (e.g. /check 5)"),
        ("/bans", _bans, "banned accounts + captcha / Steam Guard prompts"),
    ]),
    ("📊 Compare", [
        ("/compare <a> <b>", _compare, "two bots side by side (e.g. /compare 3 4)"),
    ]),
    ("🛠 Self-improve (admin)", [
        ("/improve <what>", _improve, "change my own code / settings, then restart"),
    ]),
    ("🗂 Other", [
        ("/whatsnew", _whatsnew, "anything unread across the account"),
    ]),
]

# Aliases -> canonical command name (AI commands only).
ALIASES = {
    "drops": "today",   # today's drops view
}

# --- ⚡ deterministic commands (NO model) -----------------------------------
# Answered straight from the roster scan / the fix log / config by
# watcherdog/fast_commands.py — instant and free. Listed here so /help and the
# BotFather menu show them, but they are NOT in COMMANDS (never expanded to a
# prompt). (syntax, help-text).
FAST_MENU = [
    ("/status", "instant farm health overview (no AI)"),
    ("/problems", "only the bots erroring / stuck / dead now"),
    ("/silent", "bots quiet longer than the threshold"),
    ("/fixes", "what I auto-fixed today"),
    ("/mode", "action mode (live / dry-run) + flags"),
]
FAST_ALIASES = {"down": "problems", "health": "status"}
_FAST_CANON = {s.split()[0].lstrip("/") for s, _ in FAST_MENU}
FAST_NAMES = _FAST_CANON | set(FAST_ALIASES)


def fast_parse(text):
    """Return ``(canonical_cmd, args)`` if ``text`` is a deterministic command,
    else None. Resolves FAST_ALIASES (e.g. /down -> problems)."""
    s = _split(text)
    if not s:
        return None
    cmd = FAST_ALIASES.get(s[0], s[0])
    if cmd not in _FAST_CANON:
        return None
    return cmd, s[1]

# Built from MENU: command name -> builder.
COMMANDS = {}
for _group, _rows in MENU:
    for _syntax, _builder, _desc in _rows:
        COMMANDS[_syntax.split()[0].lstrip("/")] = _builder
for _alias, _target in ALIASES.items():
    COMMANDS[_alias] = COMMANDS[_target]

# Meta commands answered directly (not sent to the agent). /start is Telegram's
# standard first-contact command — it gets a welcome; /help and /commands show
# the menu.
HELP_NAMES = {"help", "commands", "start"}

# /job reports the bot's ACTUAL current jobs from the task store (not the model).
JOB_NAMES = {"job", "jobs"}

# /stopjobs cancels the running jobs (handled by the bot, not the model).
STOP_NAMES = {"stopjobs", "stopall", "canceljobs"}


def is_stop(text):
    """True if `text` is a stop-jobs command (handled directly by the bot)."""
    s = _split(text)
    return bool(s and s[0] in STOP_NAMES)

# /cmd, optional @botname suffix, optional args. e.g. "/check 5", "/weekly".
_CMD_RE = re.compile(r"\s*/([a-zA-Z]+)(?:@\w+)?(?:\s+(.*))?$", re.S)


def _split(text):
    """Raw parse: ``(cmd_lower, args_str)`` for any ``/word…``, else None."""
    m = _CMD_RE.match(text or "")
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def parse(text):
    """Return ``(cmd, args_str)`` if ``text`` is a KNOWN farm command, else None.

    (Meta commands like /help are not returned here — see :func:`static_reply`.)
    """
    s = _split(text)
    if not s or s[0] not in COMMANDS:
        return None
    return s


def expand(text, cfg=None):
    """Expand a farm slash-command into an agent prompt. Returns None if ``text``
    is not a recognized command (caller falls back to normal handling)."""
    parsed = parse(text)
    if not parsed:
        return None
    cmd, args = parsed
    return COMMANDS[cmd](args, cfg)


def build_help():
    """The grouped command menu, as a single message for /help."""
    lines = ["🐕 WatcherDog commands", ""]
    for group, rows in MENU:
        lines.append(group)
        for syntax, _builder, desc in rows:
            lines.append(f"  {syntax} — {desc}")
        lines.append("")
    lines.append("⚡ Instant (no AI)")
    for syntax, desc in FAST_MENU:
        lines.append(f"  {syntax} — {desc}")
    lines.append("")
    # Note the aliases and the meta commands.
    lines.append("aliases: /drops = /today · /down = /problems · /health = /status")
    lines.append("/job — what I'm working on right now")
    lines.append("/stopjobs — cancel everything I'm running")
    lines.append("/help (/commands) — this list")
    return "\n".join(lines).rstrip()


def no_ai_reply(text=None):
    """Fallback for free-form/model-backed requests when DISABLE_AI=true."""
    return (
        "AI disabled. I can still run deterministic automation and commands: "
        "/status, /problems, /silent, /fixes, /mode, /help. "
        "For weekly drop collection, send: drop stats."
    )


# Friendly one-line titles for the live status message, per command. A bare
# number after a command (e.g. /check 5) is folded into the title.
_TITLES = {
    "weekly": "🗓 Compiling the weekly report",
    "today": "📅 Pulling today's summary",
    "top": "🏆 Finding the top performers",
    "worst": "🐌 Finding the laggards",
    "value": "💰 Tallying the total value",
    "problems": "🩺 Scanning for problems",
    "silent": "🤫 Checking for silent bots",
    "check": "🔬 Deep-diving a bot",
    "bans": "🚫 Checking bans & captchas",
    "compare": "⚖️ Comparing bots",
    "whatsnew": "🆕 Catching up on what's new",
    "drops": "📅 Pulling today's summary",
    "down": "🩺 Scanning for problems",
}


def friendly_title(text):
    """A short, appealing title for the live status header — derived from the
    command (or the message itself for free-form requests)."""
    s = _split(text)
    if s and s[0] in _TITLES:
        title = _TITLES[s[0]]
        if s[1]:
            title += f" ({s[1].strip()[:20]})"
        return title
    if s and s[0] in COMMANDS:
        return f"⚙️ Running /{s[0]}"
    brief = " ".join((text or "").split())[:60]
    return f"💭 Working on: {brief}" if brief else "💭 Working on it"


def build_welcome():
    """The /start greeting — a short intro followed by the command menu."""
    intro = (
        "👋 I'm WatcherDog — I watch the SinFermera farm bots for you.\n"
        "Send a command below, or just ask me in plain English "
        "(e.g. \"how are the farms?\").\n"
    )
    return intro + "\n" + build_help()


def build_jobs(cfg=None):
    """A live list of the bot's current jobs, read straight from the task store
    (the action tasks it's working on / has queued). Pure file read — no model.

    Returns a short message: one line per job with its id and latest progress, or
    a one-liner when nothing is running."""
    path = getattr(cfg, "bot_task_path", None)
    tasks = []
    if path:
        try:
            tasks = task_store.active(path)
        except Exception:  # noqa: BLE001
            tasks = []
    if not tasks:
        return "🧰 No active jobs right now."
    lines = [f"🧰 Active jobs ({len(tasks)}):"]
    for t in tasks:
        req = " ".join((t.get("request") or "").split())[:60] or "(task)"
        progress = t.get("progress") or []
        last = progress[-1] if progress else "starting…"
        lines.append(f"• #{t.get('id')} {req} — {last}")
    return "\n".join(lines)


def static_reply(text, cfg=None):
    """Direct (non-agent) reply for meta commands — or None.

    /start → welcome; /help, /commands → the menu; /job(s) → current jobs."""
    s = _split(text)
    if not s:
        return None
    cmd = s[0]
    if cmd in JOB_NAMES:
        return build_jobs(cfg)
    if cmd not in HELP_NAMES:
        return None
    if cmd == "start":
        return build_welcome()
    return build_help()


def names():
    """Sorted recognized command names (farm + fast + aliases + meta)."""
    return sorted(set(COMMANDS) | FAST_NAMES | HELP_NAMES | JOB_NAMES)
