"""Fast, cheap pre-filter for incoming group messages.

Classifies a message into one of three buckets BEFORE spending an Ollama call:

  * "error"   — matches a strong failure indicator; always worth analyzing
  * "normal"  — matches a known-good SinFermera status pattern; skip
  * "unknown" — matches neither; analyze only if ANALYZE_UNKNOWN is on

This keeps the AI from being hammered by routine drop/match/warmup spam while
still catching novel problems.
"""

from __future__ import annotations

import re

# Strong, language-agnostic failure indicators. These should almost always be
# surfaced to the owner.
_ERROR_PATTERNS = [
    r"\berror\b", r"\bfailed\b", r"\bfailure\b", r"\bexception\b", r"\btraceback\b",
    r"\bcrash", r"\bcannot\b", r"\bunable\b", r"\bcouldn'?t\b",
    r"\bban(ned)?\b", r"\bblocked\b", r"\bsuspend", r"\bkick(ed)?\b",
    r"\bcaptcha\b", r"\bverif(y|ication)\b", r"\b2fa\b", r"steam ?guard",
    r"\btimed? ?out\b", r"\btimeout\b", r"\bdisconnect", r"\blost connection\b",
    r"\bretry(ing)?\b", r"\brestart(ing|ed)?\b", r"\bstopped\b", r"\bstuck\b",
    r"\binvalid\b", r"\bexpired\b", r"\brate ?limit", r"\bproxy\b.*\b(dead|fail|bad)\b",
    r"\blogin\b.*\bfail", r"\bauth", r"\bnot ?responding\b",
    r"\b(?:can['’]?t|cannot)\s+find\s+match\s+in\s+\d+\s+minutes?.*changing\s+batch",
    r"⚠", r"❌", r"🛑", r"‼", r"\bwarn(ing)?\b",
]

# Known-good SinFermera status lines. If a message is *only* made of these
# kinds of lines, it's routine and we skip it.
_NORMAL_PATTERNS = [
    r"collected drop", r"farmed this week", r"accounts? launched",
    r"warmup started", r"warmup (finished|done|complete)",
    r"match ended with score", r"selected drops?", r"starting next batch",
    r"\btotal\s*[:=]", r"\bcase\b", r"\bfield-tested\b", r"\bfactory new\b",
    r"\bminimal wear\b", r"\bwell-worn\b", r"\bbattle-scarred\b",
    r"\$\s*$", r"-\s*[\d.]+\$",  # price tails like "- 0.27$"
]

_ERROR_RE = re.compile("|".join(_ERROR_PATTERNS), re.IGNORECASE)
_NORMAL_RE = re.compile("|".join(_NORMAL_PATTERNS), re.IGNORECASE)

# Pull a "[SinFermera3]" style bot tag off the front of a message.
_BOT_TAG_RE = re.compile(r"\[([^\]]{1,40})\]")


def bot_name_from(text):
    """Best-effort extraction of the posting bot's name from a message."""
    m = _BOT_TAG_RE.search(text or "")
    if m:
        return m.group(1).strip()
    return "unknown-bot"


def classify(text):
    """Return 'error', 'normal', or 'unknown' for a raw message string."""
    if not text or not text.strip():
        return "normal"
    if _ERROR_RE.search(text):
        return "error"

    # Treat as normal only if every non-empty, non-tag line looks routine.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if not _is_tag_or_tree(ln)]
    if meaningful and all(_NORMAL_RE.search(ln) for ln in meaningful):
        return "normal"
    if not meaningful:
        # Only a bot tag / tree glyphs — routine chatter.
        return "normal"
    return "unknown"


def _is_tag_or_tree(line):
    """Lines that carry no signal on their own: the bare bot tag, or tree
    drawing chars used to list drops."""
    stripped = line.strip()
    if _BOT_TAG_RE.fullmatch(stripped):
        return True
    # Tree glyphs / bullets used in the drop listings.
    return all(ch in "├└┌┐│─➙→•- \t" for ch in stripped)
