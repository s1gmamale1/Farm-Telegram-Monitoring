"""Deterministic slash-commands — answered with NO model (Phase 5).

``/status``, ``/problems``, ``/silent`` read the shared :mod:`roster` scan;
``/fixes`` reads the auto-fix log; ``/mode`` reads config. None of them call the
LLM, so they're instant and free — the common triage questions stop costing a
whole agent turn each. The menu/aliases for these live in
``commands.FAST_MENU`` / ``commands.fast_parse``.
"""

from __future__ import annotations

import logging

from watcherdog import daily_report, roster

logger = logging.getLogger("watcherdog.fast_commands")


def _age(age_min):
    """A compact age label: '7m', '3h12m', or '?' for the never-seen sentinel."""
    if age_min is None or age_min >= 100000:
        return "?"
    if age_min < 90:
        return f"{age_min:.0f}m"
    h, m = divmod(int(age_min), 60)
    return f"{h}h{m:02d}m"


def _sf(num, info):
    return f"SF{num} (PC{info['pc']}, {_age(info['age_min'])})"


def _status(statuses):
    """An instant health overview — counts + the bots that aren't farming."""
    if not statuses:
        return "🐕 No farm bots resolved yet."
    farming = [n for n, i in statuses.items() if i["status"] == roster.FARMING]
    quiet = [n for n, i in statuses.items() if i["status"] == roster.QUIET]
    attn = [n for n, i in statuses.items() if i["status"] == roster.ATTENTION]
    dead = [n for n, i in statuses.items() if i["status"] == roster.DEAD]
    total = len(statuses)
    lines = [f"🐕 Farm status — {len(farming)}/{total} farming  "
             f"|  ⚠️{len(quiet)}  🔴{len(attn)}  💀{len(dead)}"]
    for label, group in (("💀 dead", dead), ("🔴 needs attention", attn),
                         ("⚠️ quiet", quiet)):
        if group:
            lines.append(f"{label}: "
                         + ", ".join(_sf(n, statuses[n]) for n in sorted(group)))
    if not (dead or attn or quiet):
        lines.append("✅ everything farming.")
    return "\n".join(lines)


def _problems(statuses):
    """Only the bots that are a problem right now (attention or dead)."""
    bad = {n: i for n, i in statuses.items()
           if i["status"] in (roster.ATTENTION, roster.DEAD)}
    if not bad:
        return "✅ No problems — every bot looks healthy."
    lines = [f"🔴 Problems — {len(bad)} bot(s):"]
    for n in sorted(bad):
        emoji = roster.status_emoji(bad[n]["status"])
        lines.append(f"{emoji} {_sf(n, bad[n])}")
    return "\n".join(lines)


def _silent(statuses, cfg):
    """Bots whose last message is older than the silence threshold."""
    thr_min = float(getattr(cfg, "silence_threshold", 1800) or 1800) / 60.0
    silent = {n: i for n, i in statuses.items() if i["age_min"] > thr_min}
    if not silent:
        return f"🔊 No bots silent past ~{thr_min:.0f}m."
    lines = [f"🔇 Silent (> ~{thr_min:.0f}m) — {len(silent)} bot(s):"]
    for n in sorted(silent, key=lambda x: -statuses[x]["age_min"]):
        lines.append(f"• {_sf(n, silent[n])}")
    return "\n".join(lines)


def _fixes(cfg):
    """Everything the watcher auto-fixed today (the AI-fix log)."""
    path = getattr(cfg, "daily_errors_path", None)
    report = daily_report.build_report(path, cleared=False)
    return report or "🔧 No fixes yet today."


def _mode(cfg, deliver):
    """The action mode + the key capability flags (mirrors the startup log)."""
    actions = bool(getattr(cfg, "agent_actions_enabled", False))
    if not actions:
        mode = "READ-ONLY (AGENT_ACTIONS_ENABLED=false)"
    elif not deliver:
        mode = "DRY-RUN (would act, nothing sent)"
    else:
        mode = "LIVE (panels can be driven)"
    on = lambda b: "on" if b else "off"  # noqa: E731
    return (f"⚙️ Mode: {mode}\n"
            f"• auto-fix router: {on(actions and deliver)}\n"
            f"• bot actions: {on(getattr(cfg, 'bot_actions_enabled', False))}\n"
            f"• self-edit: {on(getattr(cfg, 'bot_self_edit_enabled', False))}\n"
            f"• self-restart: {on(getattr(cfg, 'bot_self_restart_enabled', False))}")


async def handle(cmd, args, *, cfg, client, watch, deliver=True):
    """Run one deterministic command and return its reply text. ``cmd`` is the
    canonical name from :func:`commands.fast_parse`."""
    if cmd == "fixes":
        return _fixes(cfg)
    if cmd == "mode":
        return _mode(cfg, deliver)
    # The rest need a fresh roster scan (reads Telegram, no LLM).
    statuses = await roster.scan(client, cfg, watch or [])
    if cmd == "problems":
        return _problems(statuses)
    if cmd == "silent":
        return _silent(statuses, cfg)
    return _status(statuses)
