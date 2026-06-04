# Skill 4 — Exactly 4 accounts launched

**Rule:** every working panel must have **exactly 4 accounts launched** at all
times. **Fewer than 4 or more than 4 = an error.** Detect it, count it as an
error, and report immediately.

## Detect
- After any launch, and on each routine check: read the launched/running count
  (via **Launch...s stats** or **Look selected accounts**; if unclear, take a
  **Screenshot** and count the running clients in the image).
- `count == 4` → OK. `count != 4` → error, go below.

## When count ≠ 4
1. Log it as an error (skill 2) and start a **15-minute** timer for the panel's
   own script to self-correct.
2. Keep checking during those 15 minutes. If the count returns to 4 on its own →
   note "self-recovered", done.
3. **If still ≠ 4 after 15 minutes → take over manually:**
   - Take a **Screenshot**, read it, work out why (too few launched, a crashed
     client, or duplicates pushing it over 4).
   - **Too few:** select accounts (**Sel...10 accs** / **Select accounts
     manually**) and **Start selected accounts** until exactly 4 are up.
   - **Too many:** **Kill All CS & Steam**, then re-select 4 and **Start
     selected accounts**.
   - If you can't get it to 4 (bans, auth, hardware) → report to ibo as
     needs-human.
4. Re-verify the count is 4. Record (skill 2).

## Output
```
🐕 Panel#3 — only 2/4 launched
• waited 15m, script didn't recover
• launched 2 more → 4/4 up ✅
```
```
🐕 Panel#1 — 5/4 launched (too many)
• killed all, re-selected 4 → 4/4 ✅
```
