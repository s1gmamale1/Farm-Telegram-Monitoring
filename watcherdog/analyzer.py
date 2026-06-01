"""Send an error to a local Ollama model and get back structured analysis.

Uses Ollama's native /api/chat endpoint with format="json" so the model is
constrained to emit a JSON object. Pure stdlib (urllib) — no `requests`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("watcherdog.analyzer")

_SYSTEM_PROMPT = (
    "You are an SRE assistant that triages errors from Telegram bots. "
    "You will be given a raw error log or Python traceback. "
    "Respond ONLY with a single JSON object, no prose, with exactly these keys:\n"
    '  "severity": one of "low", "medium", "high", "critical"\n'
    '  "summary": a one-line description of what went wrong\n'
    '  "root_cause": the most likely underlying cause\n'
    '  "fix": a concrete suggested fix\n'
    "Judge severity by user impact: a crash loop or data loss is critical, "
    "a failed request is high, a recoverable warning is low."
)

# Returned when the model is unavailable or its output can't be parsed, so the
# pipeline always produces *something* actionable rather than dropping the error.
def _fallback(error_text, reason):
    return {
        "severity": "high",
        "summary": "Unanalyzed error (AI analysis unavailable)",
        "root_cause": reason,
        "fix": "Inspect the raw error excerpt below manually.",
        "_fallback": True,
    }


_MESSAGE_SYSTEM_PROMPT = (
    "You monitor status messages posted by CS2/CSGO drop-farming bots in a "
    "Telegram group. NORMAL messages report routine activity: collected drops, "
    "items/cases with prices, accounts launched, warmups started, match scores, "
    "weekly farm counts, batch progress. A PROBLEM message indicates something "
    "went wrong: errors, crashes, bans, captchas/Steam Guard, login or proxy "
    "failures, disconnects, timeouts, stuck/stopped bots, or anything abnormal.\n"
    "Respond ONLY with a single JSON object with exactly these keys:\n"
    '  "is_error": true or false (true only if this indicates a real problem)\n'
    '  "severity": "low" | "medium" | "high" | "critical"\n'
    '  "summary": one-line description of the problem (empty if is_error is false)\n'
    '  "root_cause": most likely cause (empty if not an error)\n'
    '  "fix": concrete suggested action the owner should take (empty if not an error)\n'
    "Be conservative: if the message is just routine farming activity, set "
    "is_error to false."
)


def analyze_message(message_text, *, bot_name, ollama_url, model, timeout=120.0):
    """Classify a Telegram status message as a problem (or not) and explain it.

    Returns a dict like analyze() plus an "is_error" boolean. Never raises.
    """
    user_prompt = (
        f"Posting bot: {bot_name}\n\n"
        f"Message:\n```\n{(message_text or '').strip()[:4000]}\n```"
    )
    result = _chat_json(
        _MESSAGE_SYSTEM_PROMPT, user_prompt, ollama_url=ollama_url, model=model, timeout=timeout
    )
    if result is None:
        fb = _fallback(message_text, f"Could not reach/parse Ollama at {ollama_url}")
        # Without AI we can't be sure; the rule pre-filter already flagged it, so
        # treat as a probable error rather than silently dropping it.
        fb["is_error"] = True
        return fb

    severity = str(result.get("severity", "high")).strip().lower()
    if severity not in ("low", "medium", "high", "critical"):
        severity = "high"
    return {
        "is_error": bool(result.get("is_error", False)),
        "severity": severity,
        "summary": str(result.get("summary", "")).strip(),
        "root_cause": str(result.get("root_cause", "")).strip(),
        "fix": str(result.get("fix", "")).strip(),
    }


def _chat_json(system_prompt, user_prompt, *, ollama_url, model, timeout):
    """POST a chat request with format=json; return the parsed dict or None."""
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama request failed: %s", exc)
        return None
    content = (body.get("message") or {}).get("content", "")
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Ollama did not return valid JSON")
        return None


def analyze(error_text, *, bot_name, ollama_url, model, timeout=120.0):
    """Return a dict with severity/summary/root_cause/fix.

    Never raises — on any failure it returns a high-severity fallback so the
    alert still goes out.
    """
    user_prompt = (
        f"Bot name: {bot_name}\n\n"
        f"Error log / traceback:\n```\n{error_text.strip()[:6000]}\n```"
    )
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        logger.warning("Ollama request failed: %s", exc)
        return _fallback(error_text, f"Could not reach Ollama at {ollama_url}: {exc}")
    except Exception as exc:  # noqa: BLE001 - never let analysis kill the loop
        logger.warning("Ollama request errored: %s", exc)
        return _fallback(error_text, f"Ollama request error: {exc}")

    content = (body.get("message") or {}).get("content", "")
    if not content:
        return _fallback(error_text, "Ollama returned an empty response")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Model did not return valid JSON; using raw content as summary")
        return {
            "severity": "high",
            "summary": content.strip()[:200],
            "root_cause": "",
            "fix": "",
            "_fallback": True,
        }

    severity = str(parsed.get("severity", "high")).strip().lower()
    if severity not in ("low", "medium", "high", "critical"):
        severity = "high"
    return {
        "severity": severity,
        "summary": str(parsed.get("summary", "")).strip(),
        "root_cause": str(parsed.get("root_cause", "")).strip(),
        "fix": str(parsed.get("fix", "")).strip(),
    }
