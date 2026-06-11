"""Overseer endpoint surface (Phase 5) — a local UNIX-socket JSON interface.

An external Hermes overseer (the ONLY place AI lives) observes and drives the
deterministic core through these 8 endpoints; the core imports no model. One
JSON object per line: {"id", "method", "params", "token"?} ->
{"id", "result"} | {"id", "error"}. Opt-in: the watcher only binds the socket
when OVERSEER_SOCKET is configured. Handlers call the SAME functions the
monitor loop uses — no new capability paths. Destructive presses still require
an explicit confirmed:true; dry-run propagates. Stdlib only.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime

from watcherdog import (daily_report, fleet_report, learned_fixes,
                        novel_recovery, tg_actions, tg_tools)

logger = logging.getLogger("watcherdog.overseer_api")

_MAX_LINE = 64 * 1024
_NUM_RE = re.compile(r"(\d+)")


def _entity(ctx, bot):
    """Resolve a bot name against the WATCH roster only (never a raw Telegram
    username — the stranger-DM lesson). Case-insensitive exact name, or
    matching bot number. Returns (name, entity) or (None, None)."""
    want = (bot or "").strip()
    m = _NUM_RE.search(want)
    want_num = int(m.group(1)) if m else None
    for name, ent in (ctx["state"].get("watch") or []):
        if name.casefold() == want.casefold():
            return name, ent
        nm = _NUM_RE.search(name)
        if want_num is not None and nm and int(nm.group(1)) == want_num:
            return name, ent
    return None, None


async def _h_list_flagged(ctx, params):
    tracker = ctx["state"].get("tracker")
    return tracker.novel_list() if tracker is not None else []


async def _h_resolve_flagged(ctx, params):
    tracker = ctx["state"].get("tracker")
    if tracker is None:
        raise ValueError("incident tracking disabled")
    ok = tracker.resolve_by_id(int(params["id"]),
                               str(params.get("resolution") or "overseer_resolved"))
    return {"resolved": bool(ok)}


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_AUTO_YES = {"1", "true", "yes", "y", "on"}


def _split_steps(action):
    text = (action or "")
    for arrow in ("->", "→", "\n"):
        text = text.replace(arrow, ";")
    return [s.strip() for s in text.split(";") if s.strip()]


async def _h_teach_fix(ctx, params):
    for req in ("signature", "match", "fix"):
        if not (params.get(req) or "").strip():
            raise ValueError(f"missing required param: {req}")
    # Boundary validation: JSON carries \n freely, and a newline inside any
    # field would inject extra "- key: value" lines (or whole "## blocks") into
    # the brain file — corrupting the format and bypassing the policy below.
    for key in ("signature", "match", "fix", "action", "auto", "type"):
        if _CTRL_RE.search(str(params.get(key) or "")):
            raise ValueError(f"control characters not allowed in {key!r}")
    # Policy: the overseer may TEACH destructive fixes but not mint standing
    # auto-destructive authority — auto:yes + a destructive step is refused.
    # Teach it with auto:"" instead; the first recurrence then goes through
    # auto_fix's existing needs_confirm card (the owner keeps confirm power).
    action, auto = params.get("action", ""), params.get("auto", "")
    if (auto or "").strip().lower() in _AUTO_YES and any(
            tg_actions.is_destructive(s) for s in _split_steps(action)):
        raise ValueError("auto:yes with a destructive action is not teachable "
                         "over this surface — teach it without auto, the "
                         "confirm card asks the owner on first recurrence")
    return learned_fixes.append_fix(
        getattr(ctx["cfg"], "learned_fixes_path", None),
        signature=params["signature"], match=params["match"], fix=params["fix"],
        type=params.get("type", "ai"), added_by="overseer",
        date=datetime.now().date().isoformat(),
        action=action, auto=auto)


async def _h_read_bot(ctx, params):
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    limit = int(params.get("limit", 15))
    return await tg_tools.read_history(ctx["client"], ent, limit=limit)


async def _h_list_buttons(ctx, params):
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    return await tg_actions.panel_menu(ctx["client"], ent)


async def _h_press_button(ctx, params):
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    if not ctx["deliver"]:
        raise ValueError("dry-run: refusing to press real buttons")
    button = str(params.get("button") or "")
    confirmed = bool(params.get("confirmed", False))
    res = await tg_actions.press_button(ctx["client"], ent, button,
                                        confirmed=confirmed)
    # Audit keyed on the RESULT, not the raw param: press_button matches labels
    # by prefix/substring, so the param may understate what was pressed (or the
    # press may have failed entirely — no false "ok" records).
    if isinstance(res, dict) and res.get("pressed") and res.get("destructive"):
        daily_report.record(getattr(ctx["cfg"], "daily_errors_path", None),
                            panel=name, error="overseer action",
                            fix=f"pressed {res['pressed']}", result="ok")
    return res


async def _h_run_ladder(ctx, params):
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    return await novel_recovery.attempt(ctx["client"], ctx["cfg"], name,
                                        params.get("text") or "",
                                        chat=ent, deliver=ctx["deliver"])


async def _h_get_stats(ctx, params):
    fleet = await fleet_report.snapshot(ctx["client"], ctx["cfg"],
                                        ctx["state"].get("watch") or [])
    return asdict(fleet) if is_dataclass(fleet) else fleet


_HANDLERS = {
    "list_flagged": _h_list_flagged,
    "resolve_flagged": _h_resolve_flagged,
    "teach_fix": _h_teach_fix,
    "read_bot": _h_read_bot,
    "list_buttons": _h_list_buttons,
    "press_button": _h_press_button,
    "run_ladder": _h_run_ladder,
    "get_stats": _h_get_stats,
}


def _authorized(ctx, req):
    token = getattr(ctx["cfg"], "overseer_token", "") or ""
    if not token:
        return True
    got = str(req.get("token") or "")
    # bytes compare: the str form raises TypeError on non-ASCII input (which
    # would kill the connection task — or brick auth for a non-ASCII token).
    return hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))


async def _dispatch(ctx, line):
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        return {"id": None, "error": f"bad request: {exc}"}
    rid = req.get("id")
    method = req.get("method")
    if not _authorized(ctx, req):
        logger.warning("OVERSEER unauthorized request (method=%s)", method)
        return {"id": rid, "error": "unauthorized"}
    handler = _HANDLERS.get(method)
    if handler is None:
        logger.warning("OVERSEER unknown method: %r", method)
        return {"id": rid, "error": f"unknown method: {method!r}"}
    try:
        result = await handler(ctx, req.get("params") or {})
    except Exception as exc:  # noqa: BLE001 — the surface never crashes the watcher
        logger.warning("OVERSEER %s failed: %s", method, exc)
        return {"id": rid, "error": str(exc)}
    logger.info("OVERSEER %s ok (bot=%s)", method,
                (req.get("params") or {}).get("bot", "-"))
    return {"id": rid, "result": result}


async def _handle_conn(ctx, reader, writer):
    try:
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                writer.write(b'{"id": null, "error": "request too large"}\n')
                await writer.drain()
                break
            if not line:
                break
            resp = await _dispatch(ctx, line)
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def start(*, client, cfg, state, deliver=True):
    """Bind the socket and return the asyncio server (caller owns lifetime)."""
    sock = cfg.overseer_socket
    sockdir = os.path.dirname(sock) or "."
    os.makedirs(sockdir, mode=0o700, exist_ok=True)
    try:
        os.unlink(sock)              # stale socket from a crash
    except FileNotFoundError:
        pass
    ctx = {"client": client, "cfg": cfg, "state": state, "deliver": deliver}
    # umask during bind kills the perms race (makedirs mode does NOT tighten a
    # pre-existing dir — e.g. data/ — so the socket file perms must stand alone).
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(
            lambda r, w: _handle_conn(ctx, r, w), path=sock, limit=_MAX_LINE)
    finally:
        os.umask(old_umask)
    os.chmod(sock, 0o600)
    logger.info("overseer endpoint surface listening on %s (token %s)",
                sock, "required" if getattr(cfg, "overseer_token", "") else "off")
    return server


async def serve(client, cfg, state, deliver=True):
    """Long-running wrapper for the watcher: bind and serve forever. A bind
    failure logs and returns — it never crashes the watcher."""
    try:
        server = await start(client=client, cfg=cfg, state=state, deliver=deliver)
    except Exception:  # noqa: BLE001
        logger.exception("overseer surface failed to start; continuing without it")
        return
    async with server:
        await server.serve_forever()
