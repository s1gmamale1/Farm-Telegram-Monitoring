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

# Routine, self-healing "errors" the farm recovers from on its own — e.g. a
# single account missing its drop, which is simply retried on the next batch.
# These trip the generic error classifier (they contain the word "error") but
# must NOT escalate to a HIGH alert by themselves. Extend as new benign hiccups
# are observed.
_BENIGN_ERROR_PATTERNS = [
    r"error collecting drop",
]

# Strong failure indicators that VETO the benign downgrade: if any appears in
# the same message, a real problem is happening regardless of routine chatter,
# so it must still alert. Deliberately excludes the generic tokens (error /
# failed / cannot) that the benign phrase itself contains.
_STRONG_ERROR_PATTERNS = [
    r"\bban(ned)?\b", r"\bblocked\b", r"\bsuspend", r"\bkick(ed)?\b",
    r"\bcrash", r"\btraceback\b", r"\bexception\b",
    r"\bcaptcha\b", r"\bverif(y|ication)\b", r"\b2fa\b", r"steam ?guard",
    r"\bdisconnect", r"\blost connection\b", r"\btimed? ?out\b", r"\btimeout\b",
    r"\bstuck\b", r"\bnot ?responding\b", r"\brate ?limit",
    r"\bproxy\b.*\b(dead|fail|bad)\b", r"\blogin\b.*\bfail",
    r"❌", r"🛑", r"‼",
]

_BENIGN_ERROR_RE = re.compile("|".join(_BENIGN_ERROR_PATTERNS), re.IGNORECASE)
_STRONG_ERROR_RE = re.compile("|".join(_STRONG_ERROR_PATTERNS), re.IGNORECASE)

# Pull a "[SinFermera3]" style bot tag off the front of a message.
_BOT_TAG_RE = re.compile(r"\[([^\]]{1,40})\]")


def is_benign_error(text):
    """True when a message's only failure signal is a routine, self-healing
    event (e.g. ``Error collecting drop on: <account>``) that must not escalate
    to a HIGH alert. Any strong failure indicator in the same message vetoes the
    downgrade. See :data:`_BENIGN_ERROR_PATTERNS` / :data:`_STRONG_ERROR_PATTERNS`."""
    if not text:
        return False
    if _STRONG_ERROR_RE.search(text):
        return False
    return bool(_BENIGN_ERROR_RE.search(text))


# The FSM panel's OWN watchdog notice that it has gone quiet. This is a liveness
# signal (route to a /start probe), NOT a generic error — see mcp_watcher.
_PANEL_SILENCE_SELFREPORT_RE = re.compile(
    r"has\s+not\s+sent\s+any\s+messages.*please\s+check", re.IGNORECASE | re.DOTALL)


def is_panel_silence_selfreport(text):
    """True when the panel itself reports it has gone silent ('…has not sent any
    messages… Please check it!'). Routed to the liveness/recovery path."""
    return bool(text and _PANEL_SILENCE_SELFREPORT_RE.search(text))


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
