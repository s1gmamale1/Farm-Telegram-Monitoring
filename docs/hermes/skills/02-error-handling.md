# Skill 2 — Error handling (the master loop)

**Trigger:** any error, warning, or *unusual* message from a panel — anything
that isn't a normal status update.

> **You are the NOVEL-error handler.** A deterministic router runs *before* you:
> it suppresses known noise (`action: ignore`) and auto-applies any learned fix
> that has an executable `action:`, all with no model call. So by the time you're
> asked, it's usually a **new** error with no runnable fix yet. Your job is to
> resolve it and **teach a runnable fix** so the router handles it next time.

## The loop
```
detect → look up learned fix → known? apply it : ask ibo → SAVE a runnable fix → report
```

### 1. Detect & gather context
- Note the panel and the exact message text.
- Press **Screenshot**, download it, and **read the image** before judging.

### 2. Look up a learned fix
- Open `data/hermes/learned_fixes.md`. Match the error (by its signature —
  the key phrase, not the timestamp).
- **Match found → apply the saved steps automatically. Do not ask ibo.**
  Then verify (Screenshot / re-read) and confirm exactly-4-accounts (skill 4).

### 3. No match → ask ibo
Send one short question:
```
🐕 Panel#3 — new error, no saved fix
• "<error text>"
• (screenshot read: <what you see>)
What should I do?
```
Then act on ibo's reply. ibo will answer in one of two ways:
- **"Do X"** (AI-fixable): you perform X now, then **write a new entry** to
  `learned_fixes.md` so next time is automatic.
- **"I'll fix it / a human will fix it"** (human-fixable): you do **not** act.
  Record it as human-fix in `learned_fixes.md`, keep watching that panel, and
  ping ibo when it recurs or when the panel recovers.

### 4. Record what you did
- **Every fix you applied yourself** → append one line to
  `data/hermes/daily_errors.jsonl` (format below).
- New knowledge → `learned_fixes.md`.

### 5. Report
- Tell ibo what happened in the house output format (one headline + bullets).

## Files

### `data/hermes/learned_fixes.md` (the brain — grows over time)
One block per known error. **Always set `action`** for an AI fix so the router
runs it with no model next time:
```
## <short error signature>
- match: <key phrase to detect it>
- type: ai | human
- fix: <human-readable steps>
- action: <RUNNABLE: "ignore" for known noise, or button labels joined by " -> ",
           e.g. "Kill All CS & Steam -> Sel...10 accs -> Start selected accounts">
- auto: <yes only if a destructive step (Kill/Restart/Reboot/Shutdown) may run
         automatically; otherwise omit and the owner confirms via a button>
- added: <YYYY-MM-DD by ibo>
- notes: <gotchas>
```
Use `save_fix(...)` with the `action`/`auto` arguments — don't hand-write the file.

### `data/hermes/daily_errors.jsonl` (AI-fixed log — one JSON per line)
```json
{"ts":"2026-06-02T14:03:00","panel":"Panel#3","error":"proxy timeout","fix":"restarted panel","result":"ok"}
```

## End-of-day report (and crash recovery)
- **At day's end** (or when ibo asks "today's report"): read
  `daily_errors.jsonl`, summarise to ibo, then **empty the file** once the
  report is delivered.
- **On startup**, if `daily_errors.jsonl` is **non-empty**, the PC/app went down
  before reporting → report it **immediately**, then empty the file.
- Report shape:
```
🐕 Today — 3 errors auto-fixed
• Panel#1 ×2 — proxy timeout → restarted, ok
• Panel#3 ×1 — Steam stuck → killed & relaunched, ok
(file cleared)
```

## Golden rules
- Ask **once** per unknown error, then never again — save a runnable `action`
  so the router (not the model) handles every repeat.
- Never invent a fix for an unknown error; ask.
- For a destructive step you're unsure about, don't ask in text — it's offered
  as a one-tap button anyone in the group can confirm.
- After **any** fix, re-verify the panel and the 4-accounts rule.
