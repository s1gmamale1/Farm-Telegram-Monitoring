# Phase 6 — Vision-on-Demand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This run: INLINE execution by the plan author.**

**Goal:** The overseer can SEE panels (`screenshot` endpoint, fresh capture on demand) and panel cold-cases enter its `list_flagged` queue — the core stays AI-free; with no overseer connected, behavior is unchanged.

**Architecture:** One new handler in `overseer_api.py` wrapping `tg_actions.screenshot` (roster-only, dry-run gated); one kwarg (`novel=True`) in `mcp_watcher._open_panel_incident`; docs row + vision-loop section.

**Tech Stack:** Python 3.14 stdlib. Venv `.venv/bin/python`. Green-check `pytest $(git ls-files 'tests/*.py')`. Async tests: `asyncio.run` (no pytest-asyncio).

---

## Task 1: `screenshot` endpoint

**Files:** Modify `watcherdog/overseer_api.py` (handler + `_HANDLERS` entry); Test `tests/test_overseer_api.py` (append).

- [ ] **Step 1: Failing tests** — append to `tests/test_overseer_api.py`:

```python
# --- Phase 6: screenshot endpoint ----------------------------------------------

def test_screenshot_returns_download_path(tmp_path, monkeypatch):
    async def fake_shot(client, ent, *, cfg=None, timeout=30.0):
        return {"downloaded": "/tmp/sf7.jpg", "caption": "Screenshot"}

    monkeypatch.setattr(overseer_api.tg_actions, "screenshot", fake_shot)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "SF7"})

    resp = _run_with_server(cfg, state, go)
    assert resp["result"]["downloaded"] == "/tmp/sf7.jpg"


def test_screenshot_dry_run_refuses(tmp_path, monkeypatch):
    shots = []

    async def fake_shot(*a, **k):
        shots.append(a)
        raise AssertionError("BUG: real screenshot press happened")

    monkeypatch.setattr(overseer_api.tg_actions, "screenshot", fake_shot)
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "7"})

    resp = _run_with_server(cfg, state, go, deliver=False)
    assert "dry-run" in resp["error"]
    assert shots == []               # the guard fired BEFORE any press


def test_screenshot_unknown_bot(tmp_path):
    cfg = _cfg(tmp_path)
    state = {"watch": [("SinFermera7", object())]}

    async def go(sock):
        return await _call(sock, "screenshot", {"bot": "stranger99x"})

    resp = _run_with_server(cfg, state, go)
    assert "unknown bot" in resp["error"]
```

- [ ] **Step 2: Run-fail** — `pytest tests/test_overseer_api.py -k screenshot -v` → `unknown method: 'screenshot'` in the error → assertions fail.
- [ ] **Step 3: Implement** — in `watcherdog/overseer_api.py`, after `_h_run_ladder` add:

```python
async def _h_screenshot(ctx, params):
    """Fresh panel screenshot for the overseer's vision read (Phase 6). The
    returned path is host-local — the UNIX socket guarantees same host."""
    name, ent = _entity(ctx, params.get("bot"))
    if ent is None:
        raise ValueError(f"unknown bot: {params.get('bot')!r} (not in watch roster)")
    if not ctx["deliver"]:
        raise ValueError("dry-run: refusing to press the Screenshot button")
    return await tg_actions.screenshot(ctx["client"], ent, cfg=ctx["cfg"])
```

And register it in `_HANDLERS`: `"screenshot": _h_screenshot,` (after `"run_ladder"`). Update the module docstring's "8 endpoints" → "9 endpoints".

- [ ] **Step 4: Run-pass** (3 tests), **Step 5: Mutation-verify** — disable the dry-run guard (`if False and not ctx["deliver"]:`) → dry-run test fails (the fake raises a non-"dry-run" message and asserts `shots == []`); restore. Clear `__pycache__` around sed mutations.
- [ ] **Step 6: Commit** — `git add watcherdog/overseer_api.py tests/test_overseer_api.py && git commit -m "feat(overseer): screenshot endpoint — fresh capture for the vision read (Phase 6)"`

---

## Task 2: Panel cold-cases enter the overseer queue

**Files:** Modify `watcherdog/mcp_watcher.py` (`_open_panel_incident`, ~`:351`); Test `tests/test_novel_recovery.py` (append — it owns the novel-queue wiring tests).

- [ ] **Step 1: Failing test** — append to `tests/test_novel_recovery.py`:

```python
def test_panel_cold_case_enters_overseer_queue(tmp_path):
    """Phase 6: a cold-cased panel (text exhausted — only vision can diagnose)
    must appear in novel_list(), the overseer's list_flagged queue."""
    from watcherdog.incident_tracker import IncidentTracker
    from watcherdog import mcp_watcher
    t = IncidentTracker(str(tmp_path / "i.db"))
    mcp_watcher._open_panel_incident({"tracker": t}, "SinFermera13",
                                     "panel/PC down — needs PC", now=100.0)
    queue = t.novel_list()
    assert [r["bot"] for r in queue] == ["SinFermera13"]
    assert queue[0]["source"] == "panel"
    assert queue[0]["fixable"] == 0          # followup nag baseline unchanged
    t.close()
```

- [ ] **Step 2: Run-fail** — `novel_list()` is empty → assertion fails.
- [ ] **Step 3: Implement** — in `_open_panel_incident`, change the `tracker.open` call to:

```python
    tracker.open("panel", name, f"panel:{name}", "high", summary,
                 fixable=False, novel=True, now=now)
```

And extend its docstring's final line: `Cold-cases are flagged novel=True (Phase 6) so they enter the overseer's list_flagged queue — the one place the core has exhausted text-based understanding.`

- [ ] **Step 4: Run-pass**, **Step 5: Mutation-verify** — revert `novel=True` → test fails; restore.
- [ ] **Step 6: Commit** — `git add watcherdog/mcp_watcher.py tests/test_novel_recovery.py && git commit -m "feat(monitor): panel cold-cases flagged novel — they enter the overseer queue (Phase 6)"`

---

## Task 3: Docs + holistic + reviewer + PR

**Files:** Modify `docs/wiki/reference/Overseer Endpoints.md`.

- [ ] **Step 1: Docs.** Add the endpoint row to the table:

```
| `screenshot` | `bot` | `tg_actions.screenshot` (presses the panel's Screenshot button, downloads the image) | `{"downloaded": <host-local path>, "caption": ...}`; refused under dry-run |
```

And after "The overseer loop" section add:

```
## The vision loop (Phase 6)

Panel cold-cases ("needs PC" / can't-launch episodes — the points where the
core has exhausted text-based understanding) are flagged `novel=1`, so they
appear in `list_flagged` alongside novel bot errors. The overseer then calls
`screenshot(bot)` for a FRESH capture (stale images are useless — vision data
is pulled at diagnosis time, not carried on the incident), reads the image
with its own vision model (outside this repo — the core never gains an image
dependency), and acts via `press_button` / `resolve_flagged` / `teach_fix`.
The image-only `farmed/N` total stays a `?` in reports (`get_stats` exposes
`needs_vision`); it is a standing condition, not an incident. With no overseer
connected, cold-cases remain nagged human alerts — the deterministic baseline
is unchanged.
```

- [ ] **Step 2: Full green-check** (`pytest $(git ls-files 'tests/*.py')`, exit 0) + `import watcherdog.overseer_api` + E2E: CLI `screenshot` against a live test server with a fake tg_actions (scripted).
- [ ] **Step 3: Commit docs** — `git add "docs/wiki/reference/Overseer Endpoints.md" && git commit -m "docs(overseer): screenshot endpoint + the Phase 6 vision loop"`
- [ ] **Step 4: Reviewer pass** over the branch; fix Important+; re-review.
- [ ] **Step 5: Push → PR → merge-if-clean → roadmap marker (track COMPLETE).**

---

## Self-Review (plan author)

- **Spec coverage:** §A screenshot endpoint → T1 (incl. dry-run gate + roster-only); §B cold-case novel flag → T2; §C vision-loop docs → T3; error handling mirrors existing handlers (T1 code); testing reqs incl. mutation-verification → per-task.
- **Type consistency:** `_h_screenshot(ctx, params)` matches the handler signature; `tg_actions.screenshot(client, chat, *, cfg=None, timeout=30.0)` signature respected; `_open_panel_incident(state, name, summary, now=None)` call sites unchanged.
- **No placeholders:** complete code/test/doc text in every step.
