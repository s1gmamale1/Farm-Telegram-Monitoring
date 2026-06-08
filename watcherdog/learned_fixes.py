"""WatcherDog's learned-fixes "brain" (skill 2).

Read/match/append the human-readable Markdown knowledge base at
``data/hermes/learned_fixes.md``. Each known error is one block:

    ## CS2 frozen on launch
    - match: can't start/launch farm
    - type: ai
    - fix: Kill All CS & Steam, wait 10s, Select 4/10 unfarmed, Start selected accounts
    - added: 2026-06-02 by ibo
    - notes: re-screenshot after to confirm 4 accounts up

The flow (see docs/hermes/skills/02-error-handling.md):

  * On any panel error, call :func:`find_fix` with the error text. A hit means
    "apply this automatically"; ``None`` means "ask ibo, then :func:`append_fix`
    so next time is automatic".

Content inside ``<!-- ... -->`` HTML comments is ignored, so the template and
seed examples in the file are never matched as real fixes.

Pure stdlib; the file is the source of truth so a human can read/edit it.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("watcherdog.learned_fixes")

# A fix block: "## heading" then "- key: value" lines until the next "##".
_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")
_FIELD_RE = re.compile(r"^[-*]\s*([A-Za-z_]+)\s*:\s*(.*)$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Fields we recognise inside a block (others are ignored).
#   match  — key phrase that identifies the error
#   type   — ai (auto-handle) | human (needs a person)
#   fix    — human-readable steps (free text)
#   action — MACHINE-executable steps the deterministic router runs WITHOUT the
#            LLM: "ignore" (a known no-op to suppress) or a list of panel-button
#            labels separated by ';' or '->' (e.g. "Kill All CS & Steam -> Start
#            selected accounts"). Blank = no script mapping; escalate to the AI.
#   auto   — yes/no: may the router run this automatically even though a step is
#            destructive (Kill/Restart/Reboot/Shutdown)? Default no.
_FIELDS = ("match", "type", "fix", "action", "auto", "added", "notes")
_VALID_TYPES = ("ai", "human")


def _strip_comments(text):
    """Remove ``<!-- ... -->`` blocks so the template/seed examples don't parse."""
    return _COMMENT_RE.sub("", text)


def load_fixes(path):
    """Parse the brain file into a list of fix dicts (newest blocks last).

    Each dict has: ``signature`` (the ## heading) plus any of ``match``,
    ``type``, ``fix``, ``added``, ``notes``. Missing file -> ``[]``. A block
    with no ``match`` phrase is skipped (it can never be matched against).
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        logger.warning("could not read learned fixes %s: %s", path, exc)
        return []

    fixes, current = [], None
    for line in _strip_comments(text).splitlines():
        head = _HEADING_RE.match(line)
        if head:
            if current is not None:
                fixes.append(current)
            current = {"signature": head.group(1).strip()}
            continue
        if current is None:
            continue
        field = _FIELD_RE.match(line.strip())
        if field:
            key, val = field.group(1).lower(), field.group(2).strip()
            if key in _FIELDS:
                current[key] = val
    if current is not None:
        fixes.append(current)
    return [f for f in fixes if f.get("match")]


def find_fix(text, *, path=None, fixes=None):
    """Return the learned fix whose ``match`` phrase appears in ``text``, or None.

    Matching is case-insensitive substring. When several blocks match, the one
    with the **longest** match phrase wins (most specific). Pass either a loaded
    ``fixes`` list or a ``path`` to load from.
    """
    if not text:
        return None
    if fixes is None:
        fixes = load_fixes(path)
    low = text.lower()
    best, best_len = None, -1
    for fx in fixes:
        phrase = (fx.get("match") or "").strip().lower()
        if phrase and phrase in low and len(phrase) > best_len:
            best, best_len = fx, len(phrase)
    return best


def is_human_fix(fix):
    """True if this fix needs a person (``type: human``). AI is the default."""
    return bool(fix) and (fix.get("type") or "").strip().lower() == "human"


def append_fix(path, *, signature, match, fix, type="ai", added_by="ibo",
               date="", notes="", action="", auto=""):
    """Append a new fix block to the brain file (creating it if needed).

    Returns the dict that was written. ``type`` is coerced to ``ai``/``human``
    (defaults to ``ai``). ``date`` is the YYYY-MM-DD the fix was taught; pass it
    in (callers stamp the date) — left blank if empty. ``action``/``auto`` are
    the machine-executable mapping (see :data:`_FIELDS`); both are optional and
    only written when non-blank so existing human-readable blocks stay clean.
    """
    kind = (type or "ai").strip().lower()
    if kind not in _VALID_TYPES:
        kind = "ai"
    added = f"{date} by {added_by}".strip() if date else f"by {added_by}"
    action, auto = (action or "").strip(), (auto or "").strip()

    lines = [f"\n## {signature.strip()}",
             f"- match: {match.strip()}",
             f"- type: {kind}",
             f"- fix: {fix.strip()}"]
    if action:
        lines.append(f"- action: {action}")
    if auto:
        lines.append(f"- auto: {auto}")
    lines += [f"- added: {added}", f"- notes: {notes.strip()}", ""]
    block = "\n".join(lines)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Ensure a clean separation from any existing trailing content.
        needs_nl = os.path.exists(path) and os.path.getsize(path) > 0
        with open(path, "a", encoding="utf-8") as fh:
            if needs_nl:
                fh.write("\n")
            fh.write(block.lstrip("\n"))
    except OSError as exc:
        logger.warning("could not append learned fix to %s: %s", path, exc)
    out = {"signature": signature.strip(), "match": match.strip(),
           "type": kind, "fix": fix.strip(), "added": added,
           "notes": notes.strip()}
    if action:
        out["action"] = action
    if auto:
        out["auto"] = auto
    return out
