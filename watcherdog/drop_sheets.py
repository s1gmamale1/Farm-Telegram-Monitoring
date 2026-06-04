"""Google Sheets sink for the weekly drop stats (skill 5).

A ready workspace: paste your Google service-account JSON and set the
``GSHEETS_*`` env vars, and ``append_week`` starts writing rows. Until then it
no-ops cleanly (returns ``{"ok": False, "reason": "not configured"}``) so the
caller keeps the local JSON buffer in ``data/hermes/drop_stats/<week>.json``.

No new hard dependency: the Google client is imported lazily. If it's missing,
this reports ``reason="gspread not installed"`` instead of crashing.

    pip install gspread google-auth        # when you're ready to enable it

Env (see .env.example):
    GSHEETS_CREDENTIALS=data/hermes/drop_stats/credentials.json
    GSHEETS_SHEET_ID=<spreadsheet id>
    GSHEETS_TAB=DropStats
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("watcherdog.drop_sheets")

# Column order written to the sheet. Each row dict should use these keys.
COLUMNS = ["week", "date", "panel", "drops", "items", "value", "notes"]


def _cfg():
    """Read the three settings from the environment. Returns (creds, sheet, tab)."""
    creds = os.environ.get("GSHEETS_CREDENTIALS", "").strip()
    sheet = os.environ.get("GSHEETS_SHEET_ID", "").strip()
    tab = os.environ.get("GSHEETS_TAB", "DropStats").strip() or "DropStats"
    return creds, sheet, tab


def is_configured():
    """True only when the credentials file exists and a sheet id is set."""
    creds, sheet, _ = _cfg()
    return bool(sheet) and bool(creds) and os.path.exists(creds)


def _open_worksheet():
    """Authorize and return the target worksheet (creating the tab if needed).
    Raises on any failure; callers translate that into a {'ok': False} result."""
    import gspread  # lazy: only needed when actually pushing
    from google.oauth2.service_account import Credentials

    creds_path, sheet_id, tab = _cfg()
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS)  # header
    return ws


def append_week(rows):
    """Append one row per panel to the configured sheet.

    `rows` is a list of dicts keyed by COLUMNS (missing keys -> ""). Returns a
    small status dict — never raises — so skill 5 can report and keep its buffer
    on failure:
        {"ok": True, "written": N}
        {"ok": False, "reason": "not configured" | "gspread not installed" | "<err>"}
    """
    if not rows:
        return {"ok": True, "written": 0}
    if not is_configured():
        return {"ok": False, "reason": "not configured"}
    try:
        ws = _open_worksheet()
    except ModuleNotFoundError:
        return {"ok": False, "reason": "gspread not installed"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sheets open failed: %s", exc)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    payload = [[str(r.get(col, "")) for col in COLUMNS] for r in rows]
    try:
        ws.append_rows(payload, value_input_option="USER_ENTERED")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sheets append failed: %s", exc)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "written": len(payload)}
