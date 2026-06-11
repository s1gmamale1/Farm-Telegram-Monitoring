# Phase 1 — Deterministic BotStats Parser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax.

**Goal:** Parse a panel's captured text (latest event + accounts roster + the `Drop Stats` reply)
into a typed `BotStats` with zero model calls — so Phase 2 can build deterministic reports
(`/weekly /value /top`) and drop OpenRouter from the report path. Built from REAL captured
formats (24-panel live capture, 2026-06-11).

**Architecture:** Extend the existing `watcherdog/farm_stats.py` (which already has `PanelStatus`
+ `parse_panel_status`). Add three pure functions: `parse_drop_report` (the rich `Drop Stats`
text), `parse_status_event` (the latest-message event vocabulary), and `parse_bot_stats` (combine
into the `BotStats` contract locked in the deterministic-core spec). Fail-safe throughout:
unparseable → `None`/`needs_vision`, never a fabricated number, never raises.

**Tech Stack:** Python 3 / pytest. No new dependencies. No Telegram, no model — pure functions.

**Environment (same discipline as prior phases):**
- Worktree: `git -C /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring worktree add /tmp/wd-phase1 -b feat/phase1-botstats-parser main`
- Tests use the main venv: `cd /tmp/wd-phase1 && /Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/.venv/bin/python -m pytest <args>`. NEVER bare `pytest`.
- Authoritative suite: `pytest $(git ls-files 'tests/*.py')`. Main is green (~1044 passed, 2 skipped).
- Merge: `git push` FIRST, then `gh pr merge --rebase`, then grep main for a marker.
- READ `watcherdog/farm_stats.py` + `tests/test_farm_stats.py` before editing — reuse `_clean`/`_int`/`PanelStatus` patterns.

**Spec:** `docs/superpowers/specs/2026-06-11-deterministic-core-design.md` (the `BotStats` contract).
Capture tool: `scripts/capture_panel_formats.py`. Raw samples live in (gitignored) `data/captures/`.

**REAL captured formats (the ground truth — fixtures are derived from these, account names sanitized):**

Full `Drop Stats` reply:
```
=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=

Date: 10.06.2026 - 17.06.2026
Accounts: 28

Case                      | Amount | % of drops
--------------------------+--------+-----------
Sealed Genesis Terminal   | 8      | 28.6
Dreams & Nightmares Case  | 5      | 17.9
--------------------------+--------+-----------

Skin (>0.6$)                        | Amount | Price $
------------------------------------+--------+--------
USP-S | Royal Guard (Minimal Wear)  | 1      | 4.76
M4A1-S | Rose Hex (Minimal Wear)    | 1      | 0.6
------------------------------------+--------+--------

➙ Price of all drop: ~ 31.5$.
➙ Total cases: 28 pcs.
➙ AVG price of cases/all drop: 0.81$/1.12$.
```
- A report may have a **cases table but NO skins table** (e.g. all drops were sub-0.6$).
- "Can't get drop" variant: `[Stats] Can't get drop on 2 accounts. Check them.\n\nAccounts:\n78. <acct>\n80. <acct>`
- Echo/empty variant: the press sometimes returns the panel's latest EVENT line (e.g.
  `[SinFermera4] Match ended with score 8:8`) or `""` — NO `DROP REPORT` header → no drop data.

latest_text event vocabulary (7 types, tolerant of odd casing + the `[SinFarmera5]` tag typo):
`Warmup started.` · `Match ended with score 8:8` · `Match cancelled! Restarting lobby in 50 sec.`
· `Starting lobby creation in 60 sec.` · `All N accounts launched!` · `<acct> crashed and restarted successfully!`
· `Got an error while launching accounts.`

---

### Task 1: `parse_drop_report(text) -> DropReport`

**Files:**
- Modify: `watcherdog/farm_stats.py` (add `DropReport` dataclass + `parse_drop_report`)
- Test: `tests/test_farm_stats.py`

- [ ] **Step 1: Write the failing tests** (append; reuse the file's import of `farm_stats`):

```python
FULL_REPORT = (
    "=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=\n\n"
    "Date: 10.06.2026 - 17.06.2026\nAccounts: 28\n\n"
    "Case                      | Amount | % of drops\n"
    "--------------------------+--------+-----------\n"
    "Sealed Genesis Terminal   | 8      | 28.6\n"
    "Dreams & Nightmares Case  | 5      | 17.9\n"
    "--------------------------+--------+-----------\n\n"
    "Skin (>0.6$)                        | Amount | Price $\n"
    "------------------------------------+--------+--------\n"
    "USP-S | Royal Guard (Minimal Wear)  | 1      | 4.76\n"
    "M4A1-S | Rose Hex (Minimal Wear)    | 1      | 0.6\n"
    "------------------------------------+--------+--------\n\n"
    "➙ Price of all drop: ~ 31.5$.\n"
    "➙ Total cases: 28 pcs.\n"
    "➙ AVG price of cases/all drop: 0.81$/1.12$.\n")

CASES_ONLY = (
    "=-=-= ❤️FSM PANEL | DROP REPORT❤️ =-=-=\n\n"
    "Date: 10.06.2026 - 17.06.2026\nAccounts: 8\n\n"
    "Case                      | Amount | % of drops\n"
    "--------------------------+--------+-----------\n"
    "Revolution Case           | 3      | 37.5\n"
    "--------------------------+--------+-----------\n\n"
    "➙ Price of all drop: ~ 8.9$.\n➙ Total cases: 8 pcs.\n")

CANT_GET = "[Stats] Can't get drop on 2 accounts. Check them.\n\nAccounts:\n78. acc_a\n80. acc_b"


def test_parse_drop_report_full():
    r = farm_stats.parse_drop_report(FULL_REPORT)
    assert r.value_usd == 31.5
    assert r.total_cases == 28
    assert r.accounts == 28
    assert ("Sealed Genesis Terminal", 8) in [(c.name, c.amount) for c in r.cases]
    assert any(s.name.startswith("USP-S") and s.price == 4.76 for s in r.skins)
    assert r.problems == []

def test_parse_drop_report_cases_only_no_skins():
    r = farm_stats.parse_drop_report(CASES_ONLY)
    assert r.value_usd == 8.9 and r.total_cases == 8
    assert [c.name for c in r.cases] == ["Revolution Case"]
    assert r.skins == []                      # no skins table -> empty, not a crash

def test_parse_drop_report_cant_get_is_a_problem():
    r = farm_stats.parse_drop_report(CANT_GET)
    assert r.value_usd is None and r.total_cases is None
    assert r.problems and "2 accounts" in r.problems[0]

def test_parse_drop_report_echo_or_empty_is_all_none():
    for junk in ["[SinFermera4] Match ended with score 8:8", "", "random noise"]:
        r = farm_stats.parse_drop_report(junk)
        assert r.value_usd is None and r.total_cases is None
        assert r.cases == [] and r.skins == [] and r.problems == []
```

- [ ] **Step 2:** Run, confirm FAIL (`parse_drop_report`/`DropReport` undefined):
  `…/.venv/bin/python -m pytest tests/test_farm_stats.py -k drop_report -v`

- [ ] **Step 3: Implement** in `watcherdog/farm_stats.py`:
```python
@dataclass
class DropItem:
    name: str | None = None
    amount: int | None = None
    price: float | None = None        # only set for skins

@dataclass
class DropReport:
    value_usd: float | None = None    # the panel's own "Price of all drop: ~X$"
    total_cases: int | None = None    # "Total cases: N pcs"
    accounts: int | None = None       # "Accounts: N" in the report
    cases: list = field(default_factory=list)    # DropItem(name, amount)
    skins: list = field(default_factory=list)    # DropItem(name, amount, price)
    problems: list = field(default_factory=list) # e.g. "Can't get drop on N accounts"
    raw: str = ""

_DROP_HEADER_RE = re.compile(r"DROP REPORT", re.I)
_PRICE_RE = re.compile(r"Price of all drop:\s*~?\s*([\d.]+)\s*\$", re.I)
_TOTALCASES_RE = re.compile(r"Total cases:\s*(\d+)\s*pcs", re.I)
_REPORT_ACCTS_RE = re.compile(r"^Accounts:\s*(\d+)\s*$", re.I | re.M)
_CANTGET_RE = re.compile(r"Can'?t get drop on\s+(\d+)\s+accounts?", re.I)
# a table row "Name | int | num" (cases: 2 cols after name; skins: name | amount | price)
_ROW_RE = re.compile(r"^(.*?\S)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*$", re.M)

def _table_rows(section):
    """Yield (name, amount, third) for 'Name | int | num' rows in a section."""
    for m in _ROW_RE.finditer(section):
        yield _clean(m.group(1)), int(m.group(2)), float(m.group(3))

def parse_drop_report(text):
    """Parse a panel 'Drop Stats' reply. Never raises; no DROP REPORT header and no
    'Can't get drop' line -> an all-None report (echo/empty press artifact)."""
    text = text or ""
    r = DropReport(raw=text)
    cant = _CANTGET_RE.search(text)
    if cant:
        r.problems.append(f"Can't get drop on {cant.group(1)} accounts")
    if not _DROP_HEADER_RE.search(text):
        return r                       # echo/empty -> only the problem (if any) is set
    m = _PRICE_RE.search(text); r.value_usd = float(m.group(1)) if m else None
    m = _TOTALCASES_RE.search(text); r.total_cases = int(m.group(1)) if m else None
    m = _REPORT_ACCTS_RE.search(text); r.accounts = int(m.group(1)) if m else None
    # Split into the Case section and the Skin section by their headers.
    case_hdr = re.search(r"Case\s*\|\s*Amount\s*\|\s*% of drops", text, re.I)
    skin_hdr = re.search(r"Skin[^\n|]*\|\s*Amount\s*\|\s*Price", text, re.I)
    case_start = case_hdr.end() if case_hdr else None
    skin_start = skin_hdr.end() if skin_hdr else None
    if case_start is not None:
        case_section = text[case_start:(skin_hdr.start() if skin_hdr else len(text))]
        for name, amt, _pct in _table_rows(case_section):
            r.cases.append(DropItem(name=name, amount=amt))
    if skin_start is not None:
        skin_section = text[skin_start:]
        # stop before the summary (➙ lines)
        cut = skin_section.find("➙")
        if cut != -1:
            skin_section = skin_section[:cut]
        for name, amt, price in _table_rows(skin_section):
            r.skins.append(DropItem(name=name, amount=amt, price=price))
    return r
```
NOTE: the `_ROW_RE` also matches the separator-free data rows but NOT the `---+---` separators
(they have no digits in the amount column). Verify the separator lines (`----+----`) don't match
(they won't: `\d+` requires digits). The `% of drops` 3rd column for cases is a float we discard.
CRITICAL GOTCHA: a **skin name itself contains a pipe** — `USP-S | Royal Guard (Minimal Wear)`.
The row is `USP-S | Royal Guard (Minimal Wear)  | 1      | 4.76` (THREE pipes). `_ROW_RE`'s
non-greedy `(.*?\S)` backtracks until the `(\d+)` column lands on the amount, so the name captures
the full `USP-S | Royal Guard (Minimal Wear)` — do NOT "simplify" `.*?` to `[^|]+` (that would
break on the pipe in the name). The `test_parse_drop_report_full` assertion (`USP-S` skin, price
4.76) pins this — if it fails, the pipe-in-name split broke.

- [ ] **Step 4:** Run, confirm PASS: `…/.venv/bin/python -m pytest tests/test_farm_stats.py -v`
- [ ] **Step 5:** Full tracked suite — 0 failures.
- [ ] **Step 6: Commit** `feat(farm_stats): parse_drop_report — deterministic drop value/cases/skins`.

---

### Task 2: `parse_status_event(latest_text) -> str | None`

**Files:**
- Modify: `watcherdog/farm_stats.py` (add `parse_status_event`)
- Test: `tests/test_farm_stats.py`

- [ ] **Step 1: Write failing tests:**
```python
import pytest

@pytest.mark.parametrize("text,event", [
    ("[SinFermera7] Warmup started.", "warmup"),
    ("[SinFermera8] Match ended with score 8:8", "match_ended"),
    ("[SinFermera13] Match cancelled! Restarting lobby in 50 sec.", "match_cancelled"),
    ("[SinFermera16] Starting lobby creation in 60 sec.", "lobby_creating"),
    ("[SinFermera1] All 0 accounts launched!", "launched"),
    ("[SinFermera18] lilbucket_bruh crashed and restarted successfully!", "crash_recovered"),
    ("[SinFermera15] Got an error while launching accounts.", "launch_error"),
    ("[SinFarmera5] Match ended with score 8:8", "match_ended"),   # tag typo tolerated
    ("[SInFermera15] Warmup started.", "warmup"),                  # odd casing tolerated
])
def test_parse_status_event_vocabulary(text, event):
    assert farm_stats.parse_status_event(text) == event

def test_parse_status_event_unknown_is_none():
    assert farm_stats.parse_status_event("[SinFermera2] something totally novel") is None
    assert farm_stats.parse_status_event("") is None
```

- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3: Implement** (after `parse_drop_report`):
```python
_EVENT_PATTERNS = [
    ("launch_error",    re.compile(r"error while launching", re.I)),
    ("crash_recovered", re.compile(r"crashed and restarted", re.I)),
    ("match_cancelled", re.compile(r"match cancelled", re.I)),
    ("match_ended",     re.compile(r"match ended", re.I)),
    ("lobby_creating",  re.compile(r"starting lobby creation", re.I)),
    ("warmup",          re.compile(r"warmup started", re.I)),
    ("launched",        re.compile(r"all\s+\d+\s+accounts?\s+launched", re.I)),
]

def parse_status_event(text):
    """Map a panel's latest message to a canonical event name, or None if novel.
    Order matters: error/crash signals win over the routine activity lines."""
    text = text or ""
    for name, rx in _EVENT_PATTERNS:
        if rx.search(text):
            return name
    return None
```
NOTE: order matters — `launch_error`/`crash_recovered` are checked before the routine events so a
message that contains both signals is classified by the stronger one. The bot-tag (`[…]`, incl.
the `SinFarmera` typo / odd casing) is ignored because matching is on the body substring.

- [ ] **Step 4-6:** run, pass, full suite, commit `feat(farm_stats): parse_status_event — canonical event vocabulary`.

---

### Task 3: `parse_bot_stats(record) -> BotStats`

Combine a capture record (`{panel, latest_text, accounts, stats}`) into the locked `BotStats`
contract. `value_usd`/`drops` from the drop report; `accounts_up` from the roster; farmed/total
stays `needs_vision` (image-only).

**Files:**
- Modify: `watcherdog/farm_stats.py` (add `BotStats` + `parse_bot_stats`)
- Test: `tests/test_farm_stats.py`

- [ ] **Step 1: Write failing tests:**
```python
def _record(**kw):
    base = {"panel": "SinFermera7", "latest_text": "[SinFermera7] Warmup started.",
            "accounts": ["a", "b", "c", "d"], "stats": {"Drop Stats": ""}}
    base.update(kw); return base

def test_parse_bot_stats_from_full_record():
    rec = _record(latest_text="[SinFermera10] Match ended with score 8:8",
                  accounts=["a","b","c","d"], stats={"Drop Stats": FULL_REPORT})
    s = farm_stats.parse_bot_stats(rec)
    assert s.bot == "SinFermera10"
    assert s.last_status == "match_ended"
    assert s.accounts_up == 4
    assert s.value_usd == 31.5
    assert s.drops == 28
    assert s.needs_vision is True        # farmed/total is image-only -> flagged
    assert s.data_source == "text"

def test_parse_bot_stats_no_drop_data_marks_missing():
    rec = _record(stats={"Drop Stats": "[SinFermera7] Warmup started."})  # echo, no report
    s = farm_stats.parse_bot_stats(rec)
    assert s.value_usd is None and s.drops is None
    assert s.problems == [] or isinstance(s.problems, list)
    assert s.needs_vision is True

def test_parse_bot_stats_cant_get_drop_surfaces_problem():
    rec = _record(stats={"Drop Stats": CANT_GET})
    s = farm_stats.parse_bot_stats(rec)
    assert s.problems and "Can't get drop" in s.problems[0]

def test_parse_bot_stats_never_raises_on_garbage():
    s = farm_stats.parse_bot_stats({"panel": "X"})   # missing keys
    assert s.bot == "X" and s.value_usd is None and s.accounts_up in (0, None)
```

- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3: Implement** (after `parse_status_event`):
```python
@dataclass
class BotStats:
    bot: str | None = None
    last_status: str | None = None       # parse_status_event(latest_text)
    accounts_up: int | None = None       # len(record["accounts"])
    drops: int | None = None             # DropReport.total_cases
    value_usd: float | None = None       # DropReport.value_usd
    cases: list = field(default_factory=list)
    skins: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    data_source: str = "missing"         # "text" if we parsed any drop data, else "missing"
    needs_vision: bool = False           # farmed/total is image-only -> always True for now

def parse_bot_stats(record):
    """Combine a capture record into BotStats. Never raises; missing data stays None."""
    record = record or {}
    s = BotStats(bot=record.get("panel"))
    s.last_status = parse_status_event(record.get("latest_text"))
    accts = record.get("accounts")
    s.accounts_up = len(accts) if isinstance(accts, list) else None
    drop_text = (record.get("stats") or {}).get("Drop Stats", "")
    rep = parse_drop_report(drop_text)
    s.drops = rep.total_cases
    s.value_usd = rep.value_usd
    s.cases = rep.cases
    s.skins = rep.skins
    s.problems = list(rep.problems)
    s.data_source = "text" if (rep.value_usd is not None or rep.total_cases is not None) else "missing"
    # Farmed/total (launched-accs-stats) is image-only in the live captures -> always needs vision.
    s.needs_vision = True
    return s
```

- [ ] **Step 4-6:** run, pass, full suite, commit `feat(farm_stats): parse_bot_stats — combine event+roster+drops into BotStats`.

---

### Task 4: full-suite gate, PR, holistic review, merge

- [ ] **Step 1:** `pytest $(git ls-files 'tests/*.py')` — 0 failures.
- [ ] **Step 2:** push `feat/phase1-botstats-parser`; open PR.
- [ ] **Step 3:** holistic review — focus: (a) `_ROW_RE` doesn't accidentally match summary `➙`
  lines or separator `---` rows; (b) a report with a skins table but no cases (or vice-versa)
  parses without crashing; (c) `value_usd`/`drops` are NEVER fabricated when the header is absent
  (the echo artifact must give all-None); (d) `parse_bot_stats` never raises on a partial/garbage
  record. Cross-check against 2-3 MORE real Drop Stats blocks: the worktree's `data/captures/` is
  gitignored/empty, but the REAL captures are present in the MAIN checkout at the absolute path
  `/Users/aisigma/projects/csGrind/Farm-Telegram-Monitoring/data/captures/*.txt` — read a few via
  `python -c "import json; print(json.load(open('<abspath>'))['stats']['Drop Stats'])"` and run
  `parse_drop_report` against them to confirm real-world coverage (esp. the cases-only and
  echo/empty variants). Fix findings.
- [ ] **Step 4: Merge (push-first):** `git push` → `gh pr merge --rebase` → checkout+pull main →
  grep markers (`def parse_drop_report`, `def parse_status_event`, `def parse_bot_stats`, `class BotStats`).

---

## Self-review notes
- **Tiering:** Task 1 (the regex-heavy drop parser) and Task 3 (the contract combiner) get FULL
  review; Task 2 (event vocabulary) rides on mutation-verification.
- **Spec coverage:** `BotStats` shape matches the deterministic-core spec contract (every field
  Optional; `data_source`/`needs_vision`; never fabricate). `parse_drop_report` is the
  text-source for `value_usd`/`drops`; farmed/total stays `needs_vision`.
- **Fixtures:** derived from the REAL captured formats above (account names sanitized to `acc_a`
  etc.; CS skin names are public, kept). `data/captures/` stays gitignored.
- **Next (Phase 2):** wire the 9 existing `commands.py` handlers to aggregate `BotStats` across the
  fleet and format the hermes report shapes — dropping OpenRouter from the report path. Separate
  plan after this lands.
- **Watch-out:** the live capture showed `Launched accs stats` (farmed/total) is image-only —
  `needs_vision` is hardcoded True for now; a future capture that presses Screenshot + reads the
  image (overseer/Phase 6) can fill farmed/total. Don't fabricate it.
