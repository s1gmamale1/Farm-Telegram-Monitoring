# Skill 6 — Random stickers (not emoji)

After you send a **text** message to ibo, **sometimes** follow it with a
**sticker** — a real Telegram sticker, **never an emoji**.

## Rules
- Roughly **1 in 4** of your replies (random), and **only after a text reply** —
  the sticker comes second, on its own.
- Pick a **random** sticker each time (vary it; don't repeat the same one).
- Get stickers from an installed set with `get_sticker_sets`, pick one at
  random, and send it with `send_sticker`.
- **Never** on these: error reports, "needs you" / human-fix messages, or the
  weekly drop-stats report. Keep those clean.
- A sticker is a flourish, not a reply on its own. No emoji as a substitute.

## How
1. Send your text reply as normal.
2. Roll the dice (~25%). If yes:
   - `get_sticker_sets()` → choose a set → choose a random sticker.
   - `send_sticker(ibo, <sticker>)`.

> Needs `get_sticker_sets` + `send_sticker` enabled for Hermes. If they aren't,
> skip silently — never send an emoji instead.
