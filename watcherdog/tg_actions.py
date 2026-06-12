"""Action layer — let the agent DRIVE panels (skills 0 & 2), not just read.

Read paths live in ``tg_tools`` (strictly read-only). Everything here SENDS:
``/start`` a panel, press an inline button, request a screenshot. It's built on
the proven button primitives in ``drop_stats`` (``_open_menu`` / ``_await_reply``)
so behaviour matches the weekly job.

Safety (skill 2): destructive buttons — Kill / Restart / Reboot / Shutdown — are
flagged by :func:`is_destructive`. :func:`press_button` refuses them unless the
caller passes ``confirmed=True``; the agent only does that after ibo says "yes".
"""

from __future__ import annotations

import asyncio
import logging
import os

from watcherdog import farm_stats, tg_tools
from watcherdog.drop_stats import _await_reply, _open_menu

logger = logging.getLogger("watcherdog.tg_actions")

# A button label is "destructive" if it mentions any of these (case-insensitive).
# Covers the truncated labels Telegram shows, e.g. "S..own PC" for Shutdown.
DESTRUCTIVE = (
    "kill", "restart", "reboot", "shutdown", "shut down",
    "s..own", "s...own", "power off",
)


def is_destructive(label):
    """True if pressing this button is destructive (needs ibo confirmation)."""
    low = (label or "").lower()
    return any(k in low for k in DESTRUCTIVE)


def _chat_ref(ref):
    """Numeric strings -> int (a Telethon entity id); otherwise leave as-is."""
    if isinstance(ref, str) and ref.strip().lstrip("-").isdigit():
        return int(ref.strip())
    return ref


async def _resolve(client, chat):
    return await client.get_entity(_chat_ref(chat))


def _labels(message):
    """Flat list of inline-button labels on a message (skipping blanks)."""
    out = []
    for row in (getattr(message, "buttons", None) or []):
        for btn in row:
            t = (getattr(btn, "text", "") or "").strip()
            if t:
                out.append(t)
    return out


def _account_names(message):
    status = farm_stats.parse_panel_status(getattr(message, "message", "") or "")
    return [a.name for a in status.accounts if a.name]


async def panel_menu(client, chat, *, timeout=20.0):
    """``/start`` a panel and return its inline-button labels (skill 0).

    ``{"chat", "menu_message_id", "buttons":[label,...]}`` or ``{"error":...}``.
    """
    ent = await _resolve(client, chat)
    menu = await _open_menu(client, ent, timeout=timeout)
    if menu is None:
        return {"error": "no /start menu reply", "buttons": []}
    return {"chat": tg_tools.entity_name(ent), "menu_message_id": menu.id,
            "buttons": _labels(menu), "accounts": _account_names(menu),
            "text": (getattr(menu, "message", "") or "")}


async def press_button(client, chat, button, *, confirmed=False, timeout=20.0):
    """Open the panel menu and press the button whose label matches ``button``.

    Matching is case-insensitive: exact, then prefix, then substring (so the
    truncated labels in skill 0 work). Destructive buttons return
    ``{"need_confirm": True, ...}`` unless ``confirmed=True``. On success returns
    ``{"pressed", "destructive", "result"}`` with the bot's reply text.
    """
    ent = await _resolve(client, chat)
    menu = await _open_menu(client, ent, timeout=timeout)
    if menu is None:
        return {"error": "no /start menu reply"}

    want = (button or "").strip().lower()
    rows = getattr(menu, "buttons", None) or []
    match = None
    for predicate in (lambda l: l == want, lambda l: l.startswith(want),
                      lambda l: want in l):
        for row in rows:
            for btn in row:
                label = (getattr(btn, "text", "") or "").strip()
                if label and predicate(label.lower()):
                    match = (label, btn)
                    break
            if match:
                break
        if match:
            break

    if not match:
        return {"error": f"no button matching {button!r}", "buttons": _labels(menu)}

    label, btn = match
    if is_destructive(label) and not confirmed:
        return {"need_confirm": True, "button": label,
                "message": (f"'{label}' is destructive — ask ibo first, then "
                            "re-call with confirmed=true once they approve.")}

    await menu.click(text=btn.text)
    reply = await _await_reply(client, ent, menu.id, timeout=timeout)
    return {"pressed": label, "destructive": is_destructive(label),
            "result": ((reply.message or "") if reply else "")[:1500]}


_CONFIRM_LABELS = ("confirm", "yes", "✅")


async def _latest_with_buttons(client, ent, *, timeout=20.0, poll=1.0):
    """The panel's newest message carrying inline buttons, within ``timeout``
    (the confirm prompt a destructive press pops). None if none appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msgs = await client.get_messages(ent, limit=3)
        for m in (msgs or []):
            if getattr(m, "buttons", None):
                return m
        await asyncio.sleep(poll)
    return None


async def press_button_then_confirm(client, chat, button, *, timeout=20.0):
    """Press ``button``, then press the confirm button on the panel's follow-up
    prompt (the 'Reboot PC -> Confirm' flow). The CALLER is the authorization
    gate — this presses destructive buttons without asking.

    Returns ``{"pressed", "confirmed": bool, "result"}``; ``confirmed`` False
    (with ``error``) when no confirm prompt / confirm button appeared."""
    ent = await _resolve(client, chat)
    first = await press_button(client, ent, button, confirmed=True, timeout=timeout)
    if first.get("error"):
        return first
    reply = await _latest_with_buttons(client, ent, timeout=timeout)
    if reply is None:
        return {"pressed": first.get("pressed"), "confirmed": False,
                "result": first.get("result", ""),
                "error": "no confirm prompt appeared"}
    for row in (getattr(reply, "buttons", None) or []):
        for btn in row:
            label = (getattr(btn, "text", "") or "").strip().lower()
            if any(c in label for c in _CONFIRM_LABELS):
                await reply.click(text=btn.text)
                final = await _await_reply(client, ent, reply.id, timeout=timeout)
                return {"pressed": first.get("pressed"), "confirmed": True,
                        "result": ((final.message or "") if final else "")[:1500]}
    return {"pressed": first.get("pressed"), "confirmed": False,
            "result": first.get("result", ""),
            "error": "no confirm button on the prompt", "buttons": _labels(reply)}


async def send_command(client, chat, text, *, timeout=20.0):
    """Send raw text (e.g. a slash command) to a panel and return its reply."""
    ent = await _resolve(client, chat)
    sent = await client.send_message(ent, text)
    reply = await _await_reply(client, ent, sent.id, timeout=timeout)
    return {"sent": text, "result": ((reply.message or "") if reply else "")[:1500]}


async def screenshot(client, chat, *, cfg=None, timeout=30.0):
    """Press *Screenshot*, download the image, and return its path + caption.

    The path is saved under ``data/hermes/screenshots/``. Note: actually *reading*
    the image requires a vision-capable model — text models get the path only.
    """
    ent = await _resolve(client, chat)
    menu = await _open_menu(client, ent, timeout=20.0)
    if menu is None:
        return {"error": "no /start menu reply"}
    # Match like press_button: case-insensitive SUBSTRING, so an emoji/word
    # prefix ("🖼 Screenshot") still resolves. `startswith` was too strict and
    # missed the real button label on the live panels.
    target = None
    for row in (getattr(menu, "buttons", None) or []):
        for btn in row:
            if "screenshot" in (getattr(btn, "text", "") or "").lower():
                target = btn
                break
        if target:
            break
    if target is None:
        return {"error": "no Screenshot button", "buttons": _labels(menu)}
    await menu.click(text=target.text)

    reply = await _await_reply(client, ent, menu.id, timeout=timeout)
    if reply is None:
        return {"error": "no screenshot reply"}
    path = None
    if getattr(reply, "media", None):
        root = getattr(cfg, "root", ".") if cfg else "."
        d = os.path.join(root, "data", "hermes", "screenshots")
        os.makedirs(d, exist_ok=True)
        safe = tg_tools.entity_name(ent).replace("/", "_").replace(" ", "_")
        path = await reply.download_media(file=os.path.join(d, f"{safe}-{reply.id}"))
    return {"downloaded": path, "caption": (reply.message or "")[:500],
            "note": ("image saved" if path else "no media in reply") +
                    "; visual reading needs a vision model"}
