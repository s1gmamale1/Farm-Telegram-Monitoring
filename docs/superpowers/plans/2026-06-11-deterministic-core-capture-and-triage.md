# Deterministic Core — Capture Tool + Phase 3 Triage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the two now-buildable pieces of the AI-removal track: (1) a read-only **capture
tool** that dumps real panel message formats so Phases 1–2 can be built from ground truth, and
(2) **Phase 3 deterministic triage** — accurate, model-free severity (`severity_of`/`summarize`)
that replaces the crude blanket-"high" synthesis, made the default so Ollama leaves the hot path.

**Architecture:** Phase 3 builds on what already exists — `classifier.py` has the
`_STRONG_ERROR_RE`/`_BENIGN_ERROR_RE` families and `_evaluate_bot` already has a `DISABLE_AI`
deterministic branch that synthesizes `{"severity":"high", summary:text[:200]}`. We add
`severity_of(text)`/`summarize(text)` to `classifier.py`, wire them into that branch (replacing
the crude synthesis), and flip `DISABLE_AI` to default ON so the deterministic path is the
default. The capture tool is a standalone read-only script. Spec:
`docs/superpowers/specs/2026-06-11-deterministic-core-design.md`.

**Tech Stack:** Python 3 / Telethon / pytest. No new dependencies.

**Environment (same discipline as the A–D campaign):**
- Worktree: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-detcore -b feat/deterministic-core-capture-triage main`
- Tests use the main venv: `cd /tmp/wd-detcore && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`. NEVER bare `pytest`.
- Authoritative suite: `pytest $(git ls-files 'tests/*.py')`. Main is green (1026 passed, 2 skipped).
- Merge: `git push` FIRST, then `gh pr merge --rebase`, then grep main for a marker.
- READ the target test file before adding tests; reuse existing helpers.

**Key facts (verified against main `d69295e`):**
- `watcherdog/classifier.py`: `classify(text) -> "error"|"normal"|"unknown"` (line 108);
  `is_benign_error(text)` (76); `_STRONG_ERROR_RE` (70, the critical-signal family:
  ban/captcha/steam-guard/crash/traceback/disconnect/timeout/proxy-dead/login-fail/❌🛑‼);
  `_BENIGN_ERROR_RE` (69, "error collecting drop"); `_ERROR_RE` (43). `tests/test_classifier.py`
  is the test home (uses `@pytest.mark.parametrize`).
- `watcherdog/mcp_watcher.py:_evaluate_bot` (~782): the `DISABLE_AI` branch (~816-818) synthesizes
  `analysis = {"is_error": True, "severity": "high", "summary": text.strip()[:200], "root_cause":
  "", "fix": ""}`; the Ollama branch (~819-822) is the `else`. The novel-error model call is
  `if (not cfg.disable_ai) and ...: _incident_via_agent` else `_alert` (~932-936). `SEVERITY_ORDER`
  and `is_benign_error` are imported at the top of `mcp_watcher.py`.
- `watcherdog/config.py:374`: `self.disable_ai = get("DISABLE_AI", "false")...`; line 375-376 forces
  `analyze_unknown=False` when disable_ai. The owner's running banner shows `agent=deepseek` — the
  model is on the hot path today; flipping the default is the Phase-3 removal.
- `watcherdog/roster.py`: `scan(client, cfg, watch)` reads each bot's latest message (the capture
  tool reuses the roster/`tg_actions` read+button layer). `watcherdog/tg_actions.py` has
  `panel_menu`/`press_button`; `watcherdog/tg_tools.py:latest_message`.

---

## Part A — Capture tool (Phase 0; unblocks Phases 1–2)

### Task 1: read-only panel-format capture script

A standalone script that, for each panel in the watch folder, reads its latest message, sends
`/start`, captures the menu reply + button labels, and writes everything raw to
`data/captures/<panel>.txt`. No fixing, no model, no incident writes. The owner runs it once;
the output becomes Phase-1 fixtures.

**Files:**
- Create: `scripts/capture_panel_formats.py`
- Create: `tests/test_capture_panel_formats.py`

- [ ] **Step 1: Write the failing test** (the capture LOGIC is a pure function over a fake client
  so it's testable without Telegram). Create `tests/test_capture_panel_formats.py`:

```python
import asyncio
from types import SimpleNamespace

import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location(
    "capture_panel_formats",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "capture_panel_formats.py")
capture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(capture)


def test_capture_one_panel_collects_text_menu_and_buttons():
    async def fake_latest(client, ent, mark_read=False):
        return "📊 Panel status: Launched: 4 accounts", object()

    async def fake_menu(client, ref, *, timeout=20.0):
        return {"accounts": ["acc1", "acc2"], "buttons": ["Launchers stats", "Screenshot"]}

    record = asyncio.run(capture.capture_one(
        client=object(), name="SinFermera7", ent=object(),
        latest_message=fake_latest, panel_menu=fake_menu))

    assert record["panel"] == "SinFermera7"
    assert "Launched: 4 accounts" in record["latest_text"]
    assert record["buttons"] == ["Launchers stats", "Screenshot"]
    assert record["accounts"] == ["acc1", "acc2"]


def test_capture_one_degrades_on_unreadable_panel():
    async def fake_latest(client, ent, mark_read=False):
        raise RuntimeError("read failed")

    async def fake_menu(client, ref, *, timeout=20.0):
        return {"error": "no reply"}

    record = asyncio.run(capture.capture_one(
        client=object(), name="SinFermera2", ent=object(),
        latest_message=fake_latest, panel_menu=fake_menu))

    assert record["panel"] == "SinFermera2"
    assert record["latest_text"] == ""          # unreadable → empty, not a crash
    assert record.get("menu_error") == "no reply"
```

- [ ] **Step 2: Run, confirm it FAILS** (script doesn't exist):
  `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_capture_panel_formats.py -v`

- [ ] **Step 3: Implement `scripts/capture_panel_formats.py`.** The pure `capture_one` takes the
  read functions as params (dependency injection → testable); `main()` wires the real
  `tg_tools.latest_message` / `tg_actions.panel_menu` and writes files. Keep it read-only:

```python
"""Read-only capture of real panel message formats → data/captures/<panel>.txt.

Run ONCE against the live fleet to collect ground-truth samples for the
deterministic farm-stats parser (Phase 1). It only READS: latest message,
/start menu, button labels. No fixing, no model, no incident writes.

    python -m scripts.capture_panel_formats            # writes data/captures/
"""
from __future__ import annotations

import asyncio
import json
import os


async def capture_one(client, name, ent, *, latest_message, panel_menu):
    """Capture one panel's observable formats. Never raises — an unreadable panel
    degrades to empty/error fields so one dead panel can't abort the sweep."""
    try:
        text, _date = await latest_message(client, ent)
    except Exception as exc:  # noqa: BLE001
        text = ""
    record = {"panel": name, "latest_text": text or "", "buttons": [], "accounts": []}
    try:
        menu = await panel_menu(client, ent)
    except Exception as exc:  # noqa: BLE001
        menu = {"error": str(exc)}
    if menu.get("error"):
        record["menu_error"] = menu["error"]
    record["buttons"] = menu.get("buttons") or []
    record["accounts"] = menu.get("accounts") or []
    return record


async def main():  # pragma: no cover (live Telegram entrypoint)
    from watcherdog.config import load_config
    from watcherdog import tg_tools, tg_actions
    from watcherdog.mcp_watcher import load_watch_chats, connect_client  # whichever the repo uses

    cfg = load_config()
    out_dir = os.path.join(cfg.root, "data", "captures")
    os.makedirs(out_dir, exist_ok=True)
    client = await connect_client(cfg)          # adapt to the repo's actual connect helper
    watch = await load_watch_chats(client, cfg)
    for name, ent in watch:
        rec = await capture_one(client, name, ent,
                                latest_message=tg_tools.latest_message,
                                panel_menu=tg_actions.panel_menu)
        path = os.path.join(out_dir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"captured {name} -> {path}")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
```
NOTE for the implementer: `main()` is `# pragma: no cover` (live Telegram). Before finalizing,
GREP the repo for the real connect helper + `load_watch_chats` signature and the real
`tg_actions.panel_menu` signature (it may be `panel_menu(client, target, *, timeout=...)`); wire
`main()` to the actual functions. Only `capture_one` is unit-tested; `main()` is verified by
inspection. Do NOT import anything that triggers a model call.

- [ ] **Step 4: Run, confirm PASS:**
  `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest tests/test_capture_panel_formats.py -v`
- [ ] **Step 5: Full tracked suite — 0 failures.**
- [ ] **Step 6: Commit** `feat(capture): read-only panel-format capture tool for Phase-1 fixtures`.

---

## Part B — Phase 3 deterministic triage

### Task 2: `severity_of(text)` — deterministic severity from the regex families

Replaces the blanket-"high" synthesis. Critical/high signals (ban/captcha/steam-guard/crash/…)
map to `critical`; a benign self-healing hiccup maps to `low`; a generic error maps to `high`;
a non-error maps to `None`.

**Files:**
- Modify: `watcherdog/classifier.py` (add `severity_of`)
- Test: `tests/test_classifier.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_classifier.py`; it imports from
  `watcherdog.classifier` — add `severity_of` to that import line):

```python
from watcherdog.classifier import severity_of   # add to the existing import


@pytest.mark.parametrize("text", [
    "[SinFermera3] account BANNED on steam",
    "captcha required to continue",
    "Steam Guard code needed",
    "proxy is dead, login failed",
    "Traceback (most recent call last):",
])
def test_severity_of_strong_signals_are_critical(text):
    assert severity_of(text) == "critical"


def test_severity_of_benign_hiccup_is_low():
    assert severity_of("Error collecting drop on: acc_5") == "low"


def test_severity_of_generic_error_is_high():
    # an error indicator with no strong signal and not benign
    assert severity_of("could not launch accounts; unknown failure") == "high"


def test_severity_of_normal_message_is_none():
    assert severity_of("[SinFermera7] collected drop · AK-47 - 0.27$") is None
    assert severity_of("") is None
```

- [ ] **Step 2: Run, confirm FAIL** (`severity_of` undefined):
  `…/.venv/bin/python -m pytest tests/test_classifier.py -k severity_of -v`

- [ ] **Step 3: Implement `severity_of` in `watcherdog/classifier.py`** (after `is_benign_error`,
  reusing the existing `_STRONG_ERROR_RE`/`_BENIGN_ERROR_RE`/`classify`):

```python
def severity_of(text):
    """Deterministic severity for a message, or None if it isn't an error.

    No model. A STRONG failure signal (ban/captcha/Steam-Guard/crash/disconnect/
    proxy-dead/…) is `critical`; a routine self-healing hiccup (`is_benign_error`)
    is `low`; any other classified error is `high` (the conservative default that
    matches the old model fallback). Normal/unknown/empty → None (not an error)."""
    if not text or classify(text) != "error":
        return None
    if _STRONG_ERROR_RE.search(text):
        return "critical"
    if is_benign_error(text):
        return "low"
    return "high"
```
NOTE: `severity_of` returns None for `classify != "error"`. The `cant-find-match` line classifies
`error` but has no strong signal and isn't benign → `high` (correct: it's a real flag). Verify
against the existing `test_cant_find_match_changing_batch_is_error`.

- [ ] **Step 4: Run, confirm PASS:** `…/.venv/bin/python -m pytest tests/test_classifier.py -v`
- [ ] **Step 5: Full tracked suite — 0 failures.**
- [ ] **Step 6: Commit** `feat(classifier): deterministic severity_of() from the regex families`.

---

### Task 3: `summarize(text)` — deterministic one-line summary

Replaces `text.strip()[:200]`. Returns a short, stable summary: the matched strong-signal phrase
(or "error") + a bounded excerpt. No model.

**Files:**
- Modify: `watcherdog/classifier.py` (add `summarize`)
- Test: `tests/test_classifier.py`

- [ ] **Step 1: Write the failing tests:**

```python
from watcherdog.classifier import summarize   # add to the existing import


def test_summarize_names_the_strong_signal():
    out = summarize("[SinFermera3] account BANNED on steam")
    assert "ban" in out.lower()
    assert len(out) <= 160


def test_summarize_generic_error_is_bounded_excerpt():
    long = "could not launch accounts " * 20
    out = summarize(long)
    assert out and len(out) <= 160


def test_summarize_empty_is_empty_string():
    assert summarize("") == ""
```

- [ ] **Step 2: Run, confirm FAIL.**
- [ ] **Step 3: Implement `summarize` in `classifier.py`** (after `severity_of`):

```python
def summarize(text):
    """Deterministic one-line summary of an error message (no model). Names the
    matched strong signal when present, else returns a bounded first-line excerpt.
    Empty input → empty string."""
    if not text or not text.strip():
        return ""
    m = _STRONG_ERROR_RE.search(text)
    if m:
        signal = m.group(0)
        first = text.strip().splitlines()[0].strip()
        return f"{signal}: {first}"[:160]
    return text.strip().splitlines()[0].strip()[:160]
```

- [ ] **Step 4: Run, confirm PASS.**
- [ ] **Step 5: Full tracked suite — 0 failures.**
- [ ] **Step 6: Commit** `feat(classifier): deterministic summarize() (no model)`.

---

### Task 4: wire `severity_of`/`summarize` into `_evaluate_bot`'s deterministic branch

Replace the crude `{"severity":"high", summary:text[:200]}` synthesis with the accurate helpers,
so the deterministic path produces real severities (a captcha alerts CRITICAL, not generic high).

**Files:**
- Modify: `watcherdog/mcp_watcher.py:_evaluate_bot` (the `if cfg.disable_ai:` branch ~816-818)
- Test: `tests/test_mcp_watcher_core.py`

- [ ] **Step 1: Write the failing test** (reuse the file's `_cfg(tmp_path, {...})`, `IncidentStore`,
  `_FakeClient`, `asyncio.new_event_loop()` pattern):

```python
def test_disable_ai_uses_deterministic_severity_not_blanket_high(tmp_path, monkeypatch):
    # In DISABLE_AI mode a captcha must classify CRITICAL (via severity_of), not the
    # old blanket "high". Capture what gets recorded.
    cfg = _cfg(tmp_path, {"DISABLE_AI": "true", "AGENT_ACTIONS_ENABLED": "false",
                          "MIN_SEVERITY": "low", "DEDUPE_WINDOW": "0"})
    store = IncidentStore(str(tmp_path / "incidents.db"))
    client = _FakeClient()
    recorded = []
    orig = store.record
    def spy(bot, severity, *a, **k):
        recorded.append(severity); return orig(bot, severity, *a, **k)
    monkeypatch.setattr(store, "record", spy)

    asyncio.run(mcp_watcher._evaluate_bot(
        client, cfg, store, {}, "ibo", "SinFermera9",
        "captcha required to continue", 100.0,
        asyncio.new_event_loop(), deliver=True, ent=None))

    assert "critical" in recorded            # severity_of -> critical, not "high"
```

- [ ] **Step 2: Run, confirm FAIL** (today the disable_ai branch hardcodes "high"):
  `…/.venv/bin/python -m pytest tests/test_mcp_watcher_core.py -k deterministic_severity -v`

- [ ] **Step 3: Implement.** In `_evaluate_bot`, replace the `if cfg.disable_ai:` synthesis:

```python
    if cfg.disable_ai:
        sev = severity_of(text) or "high"   # severity_of returns None only for non-errors;
                                            # classify already said this isn't "normal", so
                                            # default to high if a downstream caller mis-routes
        analysis = {"is_error": True, "severity": sev,
                    "summary": summarize(text), "root_cause": "", "fix": ""}
```
Add `severity_of, summarize` to the `from watcherdog.classifier import ...` line at the top of
`mcp_watcher.py` (it already imports `classify`, `is_benign_error`). NOTE: the existing
`is_benign_error` downgrade block below still runs and is now redundant for the disable_ai path
(severity_of already returns "low" for benign) but HARMLESS (it only floors, never raises) — leave
it so the Ollama path keeps the downgrade. Do NOT remove it.

- [ ] **Step 4: Run, confirm PASS + the panel/core suites green:**
  `…/.venv/bin/python -m pytest tests/test_mcp_watcher_core.py tests/test_classifier.py -v`
- [ ] **Step 5: Full tracked suite — 0 failures.**
- [ ] **Step 6: Commit** `feat(monitor): deterministic triage uses severity_of/summarize (accurate severity, no model)`.

---

### Task 5: flip `DISABLE_AI` default to ON (deterministic is the default)

The runtime no longer calls a model by default; the model path becomes explicit opt-in.

**Files:**
- Modify: `watcherdog/config.py:374`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py`, reuse the
  `clean_environ` autouse fixture + `Config({...})`):

```python
def test_disable_ai_defaults_on(monkeypatch):
    monkeypatch.delenv("DISABLE_AI", raising=False)
    # Deterministic core is the default: no model on the runtime path unless opted in.
    assert Config({}).disable_ai is True
    # Explicit opt-back-in still works.
    assert Config({"DISABLE_AI": "false"}).disable_ai is False
```

- [ ] **Step 2: Run, confirm FAIL** (default is currently false):
  `…/.venv/bin/python -m pytest tests/test_config.py -k disable_ai_defaults_on -v`

- [ ] **Step 3: Implement.** `watcherdog/config.py:374`:
  `self.disable_ai = get("DISABLE_AI", "true").strip().lower() in ("1", "true", "yes")`
  Update any adjacent comment to say the deterministic core is the default (model = opt-in).

- [ ] **Step 4: Run the FULL tracked suite and triage carefully.** Flipping this default may flip
  expectations in tests that assumed the model path. Run:
  `…/.venv/bin/python -m pytest $(git ls-files 'tests/*.py') -q`
  For ANY failure: if a test relied on `disable_ai=False` implicitly, it must now set
  `DISABLE_AI=false` explicitly via its `_cfg(..., {"DISABLE_AI": "false"})` to keep testing the
  model path — fix each such test to be explicit, and report which tests changed and why. If a
  test asserted the OLD default (false), update it to the new default with justification. Do NOT
  mass-edit; inspect each.
- [ ] **Step 5: Confirm 0 failures after triage.**
- [ ] **Step 6: Commit** `feat(config): DISABLE_AI defaults ON — deterministic core is the default (model opt-in)`.

---

### Task 6: full-suite gate, PR, holistic review, merge

- [ ] **Step 1:** `pytest $(git ls-files 'tests/*.py')` — 0 failures.
- [ ] **Step 2:** push `feat/deterministic-core-capture-triage`; open PR.
- [ ] **Step 3:** holistic review — focus: (a) does flipping `DISABLE_AI` default leave any
  runtime path still calling a model unintentionally (grep `analyze_message`/`agent.answer`/
  `_incident_via_agent` reachability under the new default)? (b) `severity_of` ↔ the existing
  `is_benign_error` downgrade — no double-handling that mis-rates? (c) capture `main()` wires real
  helpers (inspection). Fix findings; re-run suite.
- [ ] **Step 4: Merge (push-first):** `git push` → `gh pr merge --rebase` → checkout+pull main →
  grep markers (`def severity_of`, `def summarize`, `DISABLE_AI", "true"`, `capture_one`).

---

## Self-review notes
- **Tiering:** Tasks 2/3 (pure functions) and Task 1 (DI'd capture) ride on mutation-verification.
  Task 4 (wires into `_evaluate_bot`, the campaign's hot function) and Task 5 (flips a production
  default — broad blast radius) get FULL review.
- **Spec coverage:** capture tool = spec §Components/Phase 0 (D1); `severity_of`/`summarize` =
  spec §"severity_of/summarize" contract; wiring + default flip = spec §"Phase 3". The shared
  `BotStats`/flagged-incident contracts are NOT implemented here (sample-blocked / Phase 4) — by
  design; this increment is capture + triage only.
- **Owner action between this and Phase 1:** run `python -m scripts.capture_panel_formats` and
  hand back `data/captures/` — those become the Phase-1 parser fixtures.
- **Watch-out:** Task 5's default flip is the riskiest step — the model path is exercised by
  several tests implicitly; they must each be made explicit (`DISABLE_AI=false`) rather than
  deleted, so the model path stays covered for the legacy/opt-in case.
