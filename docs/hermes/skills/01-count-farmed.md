# Skill 1 — Count Farmed

**Trigger:** ibo says "count farmed", "how much farmed", "farmed count", or asks
for the farmed total across panels.

## Steps
1. Get the panel roster (`get_folder(PANELS_FOLDER)`).
2. For each panel, in parallel where possible:
   - `send_message(panel, "/start")`, then press **Launch...s stats** (launchers
     stats) — or **Look selected accounts** if that's where the farmed/total
     figure shows.
   - Read the reply; pull the two numbers: **Farmed** and **Total**.
   - If the number isn't in text, press **Screenshot**, download it, read the
     image, and count from there.
3. If a panel doesn't answer within ~30s, mark it `?` and move on (don't block
   the whole report on one dead panel).

## Output (exactly this shape)
```
🐕 Farmed count
• Panel#1 — 18/40
• Panel#2 — 40/40 ✅
• Panel#3 — 0/40 ⚠
• Panel#4 — ?/40 (no reply)
Total: 58/160
```
Rules: one line per panel as **`Panel# — Farmed/Total`**. Mark a full panel
`✅`, a zero/stuck panel `⚠`, an unreachable one `?`. End with the grand total.
Nothing else unless ibo asks.
