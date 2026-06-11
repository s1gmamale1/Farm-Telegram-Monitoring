"""Deterministic recovery for NOVEL errors (Phase 4) — no model.

When an error has no learned fix, run the generic restart ladder the panel
rules already trust (kill_all -> select_unfarmed -> start_selected) instead of
asking a model to improvise. Critical-family errors (ban / captcha / Steam
Guard) are never auto-pressed — a restart can't fix them. The caller opens a
``novel=1`` incident either way; retries are paced by the incident follow-up
loop within the existing ``incident_max_fix_retries`` budget.
"""

from __future__ import annotations

import logging

from watcherdog import daily_report, panel_actions
from watcherdog.classifier import severity_of

logger = logging.getLogger("watcherdog.novel_recovery")

LADDER = ("kill_all", "select_unfarmed", "start_selected")


async def attempt(client, cfg, bot, text, *, chat=None, deliver=True):
    """Try the generic restart ladder on a novel error. Never raises.

    Returns ``{"status": "human_needed"|"skipped"|"attempted"|"failed", ...}``.
    The critical-family gate runs FIRST — even on a dry run — because a ban /
    captcha / Steam Guard prompt is a human problem regardless of capabilities.
    """
    if severity_of(text) == "critical":
        return {"status": "human_needed"}
    if not getattr(cfg, "novel_recovery", True):
        return {"status": "skipped", "reason": "NOVEL_RECOVERY off"}
    if not (getattr(cfg, "agent_actions_enabled", False) and deliver):
        return {"status": "skipped", "reason": "actions disabled / dry-run"}
    target = chat if chat is not None else bot
    try:
        results = await panel_actions.run_sequence(
            client, target, list(LADDER), cfg, confirmed=True)
    except Exception:  # noqa: BLE001
        logger.exception("novel ladder raised for %s", bot)
        results = [{"ok": False, "detail": {"error": "exception"}}]
    ok = len(results) == len(LADDER) and all(r.get("ok") for r in results)
    fix_desc = " -> ".join(LADDER)
    summary = " ".join((text or "").split())[:80]
    daily_report.record(getattr(cfg, "daily_errors_path", None), panel=bot,
                        error=f"novel: {summary}", fix=fix_desc,
                        result="ok" if ok else "failed")
    if ok:
        logger.info("NOVEL-LADDER %s — %s (no AI)", bot, fix_desc)
        return {"status": "attempted", "steps": list(LADDER), "results": results}
    failed_step = LADDER[max(0, len(results) - 1)] if results else LADDER[0]
    logger.warning("NOVEL-LADDER %s — failed at %s", bot, failed_step)
    return {"status": "failed", "steps": list(LADDER), "results": results,
            "failed_step": failed_step}
