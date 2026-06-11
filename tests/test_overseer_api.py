"""Tests for watcherdog.overseer_api — the Phase 5 endpoint surface."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types

from watcherdog import overseer_api
from watcherdog.incident_tracker import IncidentTracker


def _short_sock():
    """macOS caps AF_UNIX paths at ~104 bytes; pytest tmp_path is too deep.
    Use a short /tmp dir for the socket file only."""
    return os.path.join(tempfile.mkdtemp(prefix="ov.", dir="/tmp"), "s")


def _cfg(tmp_path, **kw):
    base = dict(overseer_socket=_short_sock(), overseer_token="",
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
