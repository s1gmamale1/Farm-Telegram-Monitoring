# Phase 5 — Overseer Endpoint Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This run: INLINE execution by the plan author.**

**Goal:** An opt-in local UNIX-socket JSON surface (8 endpoints) through which an external Hermes overseer observes the fleet, drives panels, teaches fixes, and closes `novel=1` incidents — core imports zero AI.

**Architecture:** New `watcherdog/overseer_api.py`: `asyncio.start_unix_server`, one JSON object per line, token auth (`hmac.compare_digest`), handler table calling the SAME functions the loop uses (`tracker.novel_list`/`resolve_by_id`, `tg_tools.read_history`, `tg_actions.panel_menu`/`press_button`, `novel_recovery.attempt`, `fleet_report.snapshot`, `learned_fixes.append_fix`). Started from `mcp_watcher` main only when `OVERSEER_SOCKET` is set. Reference client `scripts/overseer_cli.py`.

**Tech Stack:** Python 3.14 stdlib (asyncio, json, hmac, os, dataclasses.asdict). Venv `.venv/bin/python`. Green-check `pytest $(git ls-files 'tests/*.py')`. Async tests: sync `def test_` + `asyncio.run`. Real `IncidentTracker`/fixes files in tests; fakes only for the Telegram layers.

---

## Task 1: `tracker.resolve_by_id`

**Files:** Modify `watcherdog/incident_tracker.py` (next to `escalate_by_id`); Test `tests/test_incident_tracker.py` (append).

- [ ] **Step 1: Failing test** — append:

```python
def test_resolve_by_id_only_open_rows(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    row = t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird",
                 fixable=True, novel=True, now=100.0)
    assert t.resolve_by_id(row["id"], "overseer_fixed", now=200.0) is True
    assert t.open_for_bot("bot_error", "SF7") is None          # closed
    assert t.resolve_by_id(row["id"], "again", now=300.0) is False  # already resolved
    t.close()
```

- [ ] **Step 2: Run-fail** — `pytest tests/test_incident_tracker.py -k resolve_by_id -v` → AttributeError.
- [ ] **Step 3: Implement** — after `escalate_by_id`:

```python
    def resolve_by_id(self, incident_id, resolution, now=None):
        """Resolve ONE open row by id (the overseer's resolve_flagged). Returns
        True iff a row was actually closed; a resolved/unknown id is False."""
        if self._dry_run:
            return False
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE id = ? AND status = 'open'",
            (now, resolution, incident_id),
        )
        self.conn.commit()
        return cur.rowcount > 0
```

- [ ] **Step 4: Run-pass** then full file: `pytest tests/test_incident_tracker.py -q`.
- [ ] **Step 5: Mutation-verify** — drop `AND status = 'open'` → second-resolve assertion fails; restore.
- [ ] **Step 6: Commit** — `git add watcherdog/incident_tracker.py tests/test_incident_tracker.py && git commit -m "feat(incidents): resolve_by_id for the overseer surface"`

---

## Task 2: Config keys

**Files:** Modify `watcherdog/config.py` (next to `novel_recovery`); Test `tests/test_config.py` (append).

- [ ] **Step 1: Failing test** — append:

```python
def test_overseer_defaults_off(monkeypatch):
    monkeypatch.delenv("OVERSEER_SOCKET", raising=False)
    monkeypatch.delenv("OVERSEER_TOKEN", raising=False)
    cfg = Config({})
    assert cfg.overseer_socket == ""        # unset -> surface off
    assert cfg.overseer_token == ""
    assert Config({"OVERSEER_SOCKET": "data/overseer.sock"}).overseer_socket.endswith(
        "data/overseer.sock")
```

- [ ] **Step 2: Run-fail** → AttributeError.
- [ ] **Step 3: Implement** — after the `novel_recovery` block:

```python
        # Phase 5: opt-in overseer endpoint surface (local UNIX socket). Unset
        # (the default) = no socket, zero new surface. Token optional.
        _ovsock = get("OVERSEER_SOCKET", "").strip()
        self.overseer_socket = resolve_path(_ovsock) if _ovsock else ""
        self.overseer_token = get("OVERSEER_TOKEN", "").strip()
```

- [ ] **Step 4: Run-pass**, **Step 5: Commit** — `git commit -m "feat(config): OVERSEER_SOCKET/OVERSEER_TOKEN (opt-in, default off)"`

---

## Task 3: `overseer_api.py` — server, auth, queue endpoints

**Files:** Create `watcherdog/overseer_api.py`; Create `tests/test_overseer_api.py`.

- [ ] **Step 1: Failing tests** — create `tests/test_overseer_api.py`:

```python
"""Tests for watcherdog.overseer_api — the Phase 5 endpoint surface."""

from __future__ import annotations

import asyncio
import json
import types

from watcherdog import overseer_api
from watcherdog.incident_tracker import IncidentTracker


def _cfg(tmp_path, **kw):
    base = dict(overseer_socket=str(tmp_path / "ov.sock"), overseer_token="",
                learned_fixes_path=str(tmp_path / "fixes.md"),
                daily_errors_path=None, novel_recovery=True,
                agent_actions_enabled=True, panel_settle_seconds=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


async def _call(sock, method, params=None, token=None):
    reader, writer = await asyncio.open_unix_connection(sock)
    req = {"id": 1, "method": method, "params": params or {}}
    if token is not None:
        req["token"] = token
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line)


def _serve(cfg, state, deliver=True):
    """Start the server; return (loop_runner, sock_path). Tests drive calls via
    asyncio.run-style helper below."""
    return overseer_api.serve(client=object(), cfg=cfg, state=state, deliver=deliver)


def _run_with_server(cfg, state, coro_fn, deliver=True):
    async def main():
        server = await overseer_api.start(client=object(), cfg=cfg,
                                          state=state, deliver=deliver)
        try:
            return await coro_fn(cfg.overseer_socket)
        finally:
            server.close()
            await server.wait_closed()
    return asyncio.run(main())


def test_list_flagged_roundtrips_real_novel_row(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird", fixable=True,
           novel=True, now=100.0)
    cfg, state = _cfg(tmp_path), {"tracker": t}

    async def go(sock):
        return await _call(sock, "list_flagged")

    resp = _run_with_server(cfg, state, go)
    assert [r["bot"] for r in resp["result"]] == ["SF7"]
    t.close()


def test_token_required_when_configured(tmp_path):
    cfg, state = _cfg(tmp_path, overseer_token="s3cret"), {}

    async def go(sock):
        no = await _call(sock, "list_flagged")
        wrong = await _call(sock, "list_flagged", token="nope")
        ok = await _call(sock, "list_flagged", token="s3cret")
        return no, wrong, ok

    no, wrong, ok = _run_with_server(cfg, state, go)
    assert no["error"] == "unauthorized" and wrong["error"] == "unauthorized"
    assert "result" in ok


def test_resolve_flagged_closes_open_row_once(tmp_path):
    t = IncidentTracker(str(tmp_path / "i.db"))
    row = t.open("bot_error", "SF7", "bot_error:SF7", "high", "weird",
                 fixable=True, novel=True, now=100.0)
    cfg, state = _cfg(tmp_path), {"tracker": t}

    async def go(sock):
        first = await _call(sock, "resolve_flagged",
                            {"id": row["id"], "resolution": "overseer_fixed"})
        second = await _call(sock, "resolve_flagged",
                             {"id": row["id"], "resolution": "again"})
        return first, second

    first, second = _run_with_server(cfg, state, go)
    assert first["result"] == {"resolved": True}
    assert second["result"] == {"resolved": False}
    t.close()


def test_teach_fix_findable_by_find_fix(tmp_path):
    from watcherdog import learned_fixes
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        return await _call(sock, "teach_fix", {
            "signature": "flux desync", "match": "flux capacitor desync",
            "fix": "kill and relaunch", "action": "kill all; start selected",
            "auto": "yes"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"]["signature"] == "flux desync"
    found = learned_fixes.find_fix("[SinFermera7] flux capacitor desync",
                                   path=cfg.learned_fixes_path)
    assert found and found["signature"] == "flux desync"


def test_malformed_json_and_unknown_method_keep_serving(tmp_path):
    cfg, state = _cfg(tmp_path), {}

    async def go(sock):
        reader, writer = await asyncio.open_unix_connection(sock)
        writer.write(b"not json at all\n")
        await writer.drain()
        bad = json.loads(await reader.readline())
        writer.close()
        unknown = await _call(sock, "frobnicate")
        alive = await _call(sock, "list_flagged")
        return bad, unknown, alive

    bad, unknown, alive = _run_with_server(cfg, state, go)
    assert "error" in bad and "error" in unknown and "result" in alive
```

- [ ] **Step 2: Run-fail** — `pytest tests/test_overseer_api.py -q` → ModuleNotFoundError.
- [ ] **Step 3: Implement** — create `watcherdog/overseer_api.py`:

```python
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

from watcherdog import (fleet_report, learned_fixes, novel_recovery,
                        tg_actions, tg_tools)

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
    ok = tracker.resolve_by_id(int(params["id"]), str(params.get("resolution") or
                                                      "overseer_resolved"))
    return {"resolved": bool(ok)}


async def _h_teach_fix(ctx, params):
    for req in ("signature", "match", "fix"):
        if not (params.get(req) or "").strip():
            raise ValueError(f"missing required param: {req}")
    return learned_fixes.append_fix(
        getattr(ctx["cfg"], "learned_fixes_path", None),
        signature=params["signature"], match=params["match"], fix=params["fix"],
        type=params.get("type", "ai"), added_by="overseer",
        date=datetime.now().date().isoformat(),
        action=params.get("action", ""), auto=params.get("auto", ""))


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
    if confirmed and tg_actions.is_destructive(button):
        from watcherdog import daily_report
        daily_report.record(getattr(ctx["cfg"], "daily_errors_path", None),
                            panel=name, error="overseer action",
                            fix=f"pressed {button}", result="ok")
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
    return hmac.compare_digest(got, token)


async def _dispatch(ctx, line):
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        return {"id": None, "error": f"bad request: {exc}"}
    rid = req.get("id")
    if not _authorized(ctx, req):
        return {"id": rid, "error": "unauthorized"}
    method = req.get("method")
    handler = _HANDLERS.get(method)
    if handler is None:
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
    os.makedirs(os.path.dirname(sock) or ".", mode=0o700, exist_ok=True)
    try:
        os.unlink(sock)              # stale socket from a crash
    except FileNotFoundError:
        pass
    ctx = {"client": client, "cfg": cfg, "state": state, "deliver": deliver}
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_conn(ctx, r, w), path=sock, limit=_MAX_LINE)
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
```

- [ ] **Step 4: Run-pass** — `pytest tests/test_overseer_api.py -q` (5 tests).
- [ ] **Step 5: Mutation-verify** — (a) auth: make `_authorized` return True always → token test fails; restore. (b) resolve open-only guard already mutation-verified in Task 1.
- [ ] **Step 6: Commit** — `git add watcherdog/overseer_api.py tests/test_overseer_api.py && git commit -m "feat(overseer): UNIX-socket endpoint surface — auth, list_flagged, resolve_flagged, teach_fix"`

---

## Task 4: Action endpoints (read_bot / list_buttons / press_button / run_ladder / get_stats)

**Files:** Modify `tests/test_overseer_api.py` (append; handlers already in Task 3's module).

- [ ] **Step 1: Failing tests** — append:

```python
def test_press_button_destructive_requires_confirmed(tmp_path, monkeypatch):
    calls = []

    async def fake_press(client, ent, button, *, confirmed=False, timeout=20.0):
        calls.append((button, confirmed))
        if overseer_api.tg_actions.is_destructive(button) and not confirmed:
            return {"need_confirm": True, "button": button}
        return {"pressed": button, "destructive": False}

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        refuse = await _call(sock, "press_button",
                             {"bot": "SF7", "button": "kill all cs"})
        allow = await _call(sock, "press_button",
                            {"bot": "SF7", "button": "kill all cs", "confirmed": True})
        return refuse, allow

    refuse, allow = _run_with_server(cfg, state, go)
    assert refuse["result"].get("need_confirm") is True     # gate held
    assert allow["result"].get("pressed") == "kill all cs"
    assert calls == [("kill all cs", False), ("kill all cs", True)]


def test_press_button_dry_run_refuses(tmp_path, monkeypatch):
    async def fake_press(*a, **k):
        raise AssertionError("dry-run must not press")

    monkeypatch.setattr(overseer_api.tg_actions, "press_button", fake_press)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "press_button",
                           {"bot": "7", "button": "drop stats", "confirmed": False})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert "dry-run" in resp["error"]


def test_run_ladder_honors_attempt_gates(tmp_path, monkeypatch):
    seen = {}

    async def fake_attempt(client, cfg, bot, text, *, chat=None, deliver=True):
        seen["bot"], seen["deliver"] = bot, deliver
        return {"status": "skipped", "reason": "dry-run"}

    monkeypatch.setattr(overseer_api.novel_recovery, "attempt", fake_attempt)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "run_ladder", {"bot": "SinFermera7"})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert resp["result"]["status"] == "skipped"
    assert seen == {"bot": "SinFermera7", "deliver": False}


def test_unknown_bot_is_error_not_username_resolution(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "read_bot", {"bot": "stranger99x"})

    resp = _run_with_server(cfg, state, go)
    assert "unknown bot" in resp["error"]
```

- [ ] **Step 2: Run-fail then pass** — handlers exist from Task 3; these should pass directly. If any fail, the handler is wrong — fix the handler (TDD red came from Task 3's module-level red).
- [ ] **Step 3: Mutation-verify** — drop the `if not ctx["deliver"]` guard in `_h_press_button` → dry-run test fails; restore. Make `_entity` fall through to returning the raw name → unknown-bot test fails; restore.
- [ ] **Step 4: Commit** — `git add tests/test_overseer_api.py && git commit -m "test(overseer): action-endpoint gates (confirm, dry-run, roster-only)"`

---

## Task 5: Wiring + CLI + docs

**Files:** Modify `watcherdog/mcp_watcher.py` (import + serve task next to `_incident_followup_loop` startup ~`:1761`); Create `scripts/overseer_cli.py`; Create `docs/wiki/reference/Overseer Endpoints.md`.

- [ ] **Step 1: Wire the serve task.** Add `overseer_api` to the `from watcherdog import (...)` block. After the `_incident_followup_loop` startup block insert:

```python
        # Phase 5: opt-in overseer endpoint surface (local UNIX socket, no AI in-core).
        if cfg.overseer_socket:
            client.loop.create_task(overseer_api.serve(client, cfg, state, deliver))
```

- [ ] **Step 2: CLI** — create `scripts/overseer_cli.py`:

```python
"""Reference client for the overseer endpoint surface (Phase 5).

    OVERSEER_SOCKET=data/overseer.sock python -m scripts.overseer_cli list_flagged
    python -m scripts.overseer_cli --socket data/overseer.sock \
        press_button '{"bot": "SinFermera7", "button": "drop stats"}'

One request per invocation; token read from $OVERSEER_TOKEN when set. This is
also Hermes's integration reference: ndjson over a UNIX socket.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys


def call(sock_path, method, params, token=""):
    req = {"id": 1, "method": method, "params": params}
    if token:
        req["token"] = token
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("method")
    ap.add_argument("params", nargs="?", default="{}", help="JSON params object")
    ap.add_argument("--socket", default=os.environ.get("OVERSEER_SOCKET", ""))
    args = ap.parse_args(argv)
    if not args.socket:
        sys.exit("no socket: pass --socket or set OVERSEER_SOCKET")
    resp = call(args.socket, args.method, json.loads(args.params),
                token=os.environ.get("OVERSEER_TOKEN", ""))
    print(json.dumps(resp, indent=2, ensure_ascii=False, default=str))
    return 0 if "result" in resp else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Docs** — create `docs/wiki/reference/Overseer Endpoints.md` with the endpoint table (method/params/wraps/returns from the spec), the protocol example, auth note, and the CLI invocation above.
- [ ] **Step 4: Verify** — `python -c "import watcherdog.mcp_watcher; import scripts.overseer_cli"`; full green-check.
- [ ] **Step 5: Commit** — `git add watcherdog/mcp_watcher.py scripts/overseer_cli.py "docs/wiki/reference/Overseer Endpoints.md" && git commit -m "feat(overseer): opt-in serve wiring + reference CLI + endpoint docs"`

---

## Task 6: Holistic verify + reviewer pass + PR

- [ ] **Step 1:** Full green-check; `import` checks; grep: `overseer_api` not imported anywhere except `mcp_watcher` wiring; no model imports in `overseer_api.py`.
- [ ] **Step 2:** End-to-end DoD check: with a real tracker row + the CLI against a live test server (in a tmp dir): `list_flagged` shows it, `teach_fix` writes, `resolve_flagged` closes — scripted, not manual.
- [ ] **Step 3:** Reviewer pass (subagent) over the whole branch; fix Important+; re-review.
- [ ] **Step 4:** Push → PR → merge-if-clean → roadmap marker.

---

## Self-Review (plan author)

- **Spec coverage:** A (module+8 endpoints) → T3/T4; B (auth/safety incl. 64KB cap, audit log, dry-run) → T3 code + T4 tests; C (config+wiring) → T2/T5; D (CLI) → T5; E (docs) → T5; resolve_by_id → T1; testing reqs (real tracker/socket, mutation) → per-task.
- **Type consistency:** `start(*, client, cfg, state, deliver)` vs `serve(client, cfg, state, deliver)` used consistently (tests use `start`; watcher uses `serve`); handler signature `(ctx, params)` uniform; `_cfg` SimpleNamespace fields match every getattr in the module.
- **No placeholders:** complete code everywhere; docs page content enumerated (table from spec).
