# Skill 5 — Weekly Drop Stats (Wednesday 00:00)

**Trigger:** Wednesday at **00:00** (scheduled), or ibo says "drop stats".

## Steps
1. **Stop all farms.** For every panel: `/start` → stop the farm (e.g. **Kill
   All CS & Steam**, or the panel's stop control). Confirm each is stopped via a
   Screenshot if unsure.
2. **Request drop stats.** For each panel press **Drops Stats**; read the reply
   (and Screenshot if the numbers are only on screen).
3. **Analyse & record.** Per panel capture: drops this week, items, value, and
   anything notable. Save a row to `data/hermes/drop_stats/<YYYY-WW>.json`
   (one file per ISO week) — this is the buffer that gets pushed to Sheets.
4. **Push to Google Sheets** via `watcherdog/drop_sheets.py`:
   - If credentials are configured → append the rows. Confirm to ibo.
   - If **not** configured yet → keep the JSON buffer and tell ibo
     "saved locally, waiting on the Sheets API key" (see workspace below).
5. **Report** to ibo:
```
🐕 Weekly drops — 2026-W23
• Panel#1 — 312 drops · ~$xx
• Panel#2 — 280 drops · ~$xx
Total: 592 · saved to Sheets ✅   (or: buffered, no API key yet)
```

## Google Sheets workspace (ready for your API key)
Everything is wired so you only paste credentials, no code changes:
- Drop the service-account JSON at `data/hermes/drop_stats/credentials.json`.
- Set in `.env`:
  - `GSHEETS_CREDENTIALS=data/hermes/drop_stats/credentials.json`
  - `GSHEETS_SHEET_ID=<your spreadsheet id>`
  - `GSHEETS_TAB=DropStats`
- `watcherdog/drop_sheets.py` reads those, and `append_week(rows)` writes a row
  per panel. Until the key/ID are present it no-ops and returns
  `{"ok": false, "reason": "not configured"}` so the buffer is kept.
- Column order: `week, date, panel, drops, items, value, notes`.
