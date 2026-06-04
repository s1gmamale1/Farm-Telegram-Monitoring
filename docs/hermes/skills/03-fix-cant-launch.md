# Skill 3 — "Can't start/launch farm, check your accounts"

**Trigger:** a panel says it can't start/launch the farm, or "check your
accounts", or the farm clearly didn't come up.

This is skill 2 with a specific investigation. **Always read the screenshot.**

## Steps
1. `send_message(panel, "/start")`.
2. Press **Screenshot** → wait → `download_media` → **read the image**.
3. Diagnose from what you see. Common causes & responses:
   | What the screenshot shows | Likely cause | Action |
   |---|---|---|
   | Steam login / Steam Guard / captcha | account needs auth | record + ask ibo (usually human-fix) |
   | "no accounts selected" | nothing selected | press **Sel...10 accs** or **Sel...farmed**, then **Start selected accounts** |
   | CS2/Steam hung or frozen windows | stuck processes | **Kill All CS & Steam** → re-select → **Start selected accounts** |
   | proxy/network/disconnect error | connectivity | **Restart panel**; if still bad, ask ibo |
   | accounts banned / VAC / red | banned accounts | record + report to ibo (human-fix) |
   | can't tell | unclear | **Look selected accounts** + check learned fixes; if still unclear, ask ibo with the screenshot read |
4. Check `learned_fixes.md` first — if this exact symptom is known, apply that.
5. After acting, **re-Screenshot** and confirm the farm is up with **exactly 4
   accounts** (skill 4).
6. Record (skill 2): AI-fix → `daily_errors.jsonl`; new knowledge → `learned_fixes.md`.

## Output
```
🐕 Panel#2 — couldn't launch, fixed
• screenshot: CS2 was frozen
• Kill All CS&Steam → re-selected 4 → started → 4/4 up ✅
```
If it needs a human:
```
🐕 Panel#2 — needs you
• screenshot: Steam Guard prompt on acc #3
• can't auto-fix — login required
```

**Remember the resolution.** Once ibo confirms a fix for a symptom, you should
recognise that screenshot/error next time and fix it yourself.
