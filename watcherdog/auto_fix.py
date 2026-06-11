"""Deterministic, script-first error handling — the auto-fix router (Phase 2).

The whole point: handle errors WITHOUT calling the LLM whenever the brain
already knows what to do. Every detected error hits :func:`try_auto_fix` first.
It uses only stdlib + the existing deterministic pieces (``classify``,
``learned_fixes.find_fix``, ``tg_actions``) — no ``agent.answer``, so a known
error costs **zero tokens**. Genuinely novel errors fall through to the
deterministic novel-error ladder (``novel_recovery``, Phase 4).

Outcome contract — :func:`try_auto_fix` returns ``None`` to escalate, or a dict
``{"status": ...}`` the caller acts on:

  * ``suppressed`` — a known no-op (``action: ignore``); drop it silently.
  * ``fixed``      — executed the mapped panel actions; report what was done.
  * ``failed``     — tried but a button press errored; the caller alerts and
                    the incident follow-up loop may retry the learned fix.
  * ``human``      — a ``type: human`` fix; alert the owner, don't auto-act.
  * ``needs_confirm`` — a destructive fix not marked ``auto: yes``; escalate so
                    a one-tap confirm card can ask.

``None`` means "no learned mapping" (novel error, or a known fix with free-text
steps but no executable ``action:``) — the caller distinguishes the two with a
read-only ``find_fix`` and either runs the Phase 4 ladder (truly novel) or
plain-alerts (free-text fix a person must apply).
"""

from __future__ import annotations

import logging

from watcherdog import daily_report, learned_fixes, tg_actions
from watcherdog.classifier import classify

logger = logging.getLogger("watcherdog.auto_fix")

# Values of the `action` field that mean "known noise — do nothing".
_IGNORE = {"ignore", "none", "noop", "no-op", "skip", "suppress"}
_AUTO_YES = {"1", "true", "yes", "y", "on"}


def parse_action(action):
    """Split an ``action`` field into ordered button-label steps.

    Steps are separated by ``;``, ``->``, ``→`` or newlines. Returns ``[]`` for
    a blank or ``ignore``-class action (callers check :func:`is_ignore` first).
    """
    text = (action or "").strip()
    if not text or text.lower() in _IGNORE:
        return []
    # Normalise the arrow forms to ';' then split.
    for arrow in ("->", "→", "\n"):
        text = text.replace(arrow, ";")
    return [s.strip() for s in text.split(";") if s.strip()]


def is_ignore(fix):
    """True if this fix is an explicit known no-op (``action: ignore``)."""
    return bool(fix) and (fix.get("action") or "").strip().lower() in _IGNORE


def _auto_ok(fix):
    return (fix.get("auto") or "").strip().lower() in _AUTO_YES


async def try_auto_fix(client, cfg, bot, text, *, chat=None):
    """Handle one error deterministically if the brain knows how. No LLM.

    ``chat`` is the panel entity/ref to drive (defaults to ``bot``). Returns the
    outcome dict described in the module docstring, or ``None`` to escalate.
    """
    if not text or not text.strip():
        return None
    if classify(text) == "normal":
        return None

    fix = learned_fixes.find_fix(text, path=getattr(cfg, "learned_fixes_path", None))
    if not fix:
        return None  # novel error — caller runs the Phase 4 ladder

    if learned_fixes.is_human_fix(fix):
        return {"status": "human", "fix": fix}

    if is_ignore(fix):
        logger.info("AUTO-SUPPRESS %s — known no-op (%s)", bot, fix.get("signature"))
        return {"status": "suppressed", "fix": fix}

    steps = parse_action(fix.get("action", ""))
    if not steps:
        # Known fix but only free-text guidance — can't run it safely. The
        # caller plain-alerts so a person applies it (and saves an `action`).
        return None

    destructive = any(tg_actions.is_destructive(s) for s in steps)
    if destructive and not _auto_ok(fix):
        return {"status": "needs_confirm", "fix": fix, "steps": steps}

    # Execute the mapped steps in order against the panel — script-only.
    target = chat if chat is not None else bot
    results, ok = [], True
    for label in steps:
        res = await tg_actions.press_button(client, target, label, confirmed=destructive)
        results.append({"button": label, "result": res})
        if not isinstance(res, dict) or res.get("error") or res.get("need_confirm"):
            ok = False
            logger.warning("AUTO-FIX %s — step %r failed: %s", bot, label, res)
            break

    fix_desc = " -> ".join(steps)
    if ok:
        daily_report.record(
            getattr(cfg, "daily_errors_path", None), panel=bot,
            error=fix.get("signature", "error"), fix=fix_desc, result="ok")
        logger.info("AUTO-FIX %s — %s (no AI)", bot, fix_desc)
        return {"status": "fixed", "fix": fix, "steps": steps,
                "results": results, "summary": fix_desc}

    daily_report.record(
        getattr(cfg, "daily_errors_path", None), panel=bot,
        error=fix.get("signature", "error"), fix=fix_desc, result="failed")
    return {"status": "failed", "fix": fix, "steps": steps, "results": results}


def format_fixed(bot, outcome):
    """One-line owner report for a successful auto-fix (past tense, no question)."""
    fix = outcome.get("fix", {})
    return (f"🔧 {bot} — auto-fixed: {fix.get('signature', 'error')}\n"
            f"• {outcome.get('summary', '')} ✅")


def format_human(bot, outcome):
    """Owner alert for a known issue that needs a person (type: human)."""
    fix = outcome.get("fix", {})
    return (f"🐕 {bot} — needs you: {fix.get('signature', 'issue')}\n"
            f"• {fix.get('fix', 'a person must handle this')}")
