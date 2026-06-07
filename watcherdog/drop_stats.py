"""Weekly Drop Stats job + scheduler (skill 5).

Every **Wednesday at 00:00** (and on ibo's "drop stats" request) WatcherDog:

  1. **Stops every farm** — for each panel it opens the ``/start`` menu and
     presses *Kill All CS & Steam* (the panel's stop control).
  2. **Pulls Drops Stats** — presses *Drops Stats* and reads the reply.
  3. **Buffers** one row per panel to ``DROP_STATS_DIR/<YYYY-Www>.json`` (one
     file per ISO week — the buffer that gets pushed to Sheets).
  4. **Pushes to Google Sheets** via :func:`watcherdog.drop_sheets.append_week`
     (a no-op that keeps the buffer until the ``GSHEETS_*`` key is configured).
  5. **Reports** the week's totals to ibo.

The panel-driving runs over the watcher's own Telethon connection (send
``/start`` → ``list inline buttons`` → ``press`` → read), mirroring how the
read-only agent inspects chats. The parsing/formatting helpers are pure so they
can be unit-tested without Telegram.

See docs/hermes/skills/05-drop-stats.md and skills/00-panels.md. Pure stdlib
apart from the (already-required) Telethon client passed in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta

from watcherdog import drop_sheets, tg_tools

log = logging.getLogger("watcherdog.drop_stats")

# Button-label prefixes (labels are truncated in Telegram — match by prefix).
STOP_BUTTONS = ("kill all cs", "kill all", "stop the farm", "stop farm", "stop")
DROPS_BUTTONS = ("drops stats", "drop stats", "drops")
# Operator rule: the activity booster must run AFTER drop stats are pulled.
BOOSTER_BUTTONS = ("run activity booster", "activity booster")

# Wednesday = weekday() 2 (Mon=0). Run at 00:00.
RUN_WEEKDAY = 2
RUN_HOUR = 0
RUN_MINUTE = 0


# --- pure helpers (unit-tested) --------------------------------------------
def iso_week(dt):
    """Canonical week id like ``2026-W23`` (ISO year + zero-padded ISO week).

    Used both for the buffer filename and ibo's report header so they match.
    """
    iso = dt.isocalendar()
    year, week = iso[0], iso[1]
    return f"{year}-W{week:02d}"


def buffer_path(drop_stats_dir, week):
    """Path of the weekly buffer file for ``week`` (e.g. .../2026-W23.json)."""
    return os.path.join(drop_stats_dir, f"{week}.json")


def panel_label(name):
    """Display name -> ``Panel#N`` using the number in the name (skill 0)."""
    m = re.search(r"(\d+)", name or "")
    return f"Panel#{m.group(1)}" if m else (name.strip() if name else "Panel#?")


def parse_drop_stats(text):
    """Best-effort pull of (drops, items, value, notes) from a panel's reply.

    The control bots' exact wording isn't fixed, so each field is matched
    leniently and missing ones come back as ``""``. Returns a dict keyed for
    :data:`watcherdog.drop_sheets.COLUMNS`.
    """
    text = (text or "").strip()

    def _first(*patterns):
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1).replace(",", "")
        return ""

    # "1,204 drops" (number right before the word) is the most reliable form, so
    # try it before "Drops this week: 312" (number after, allowing a small gap).
    drops = _first(r"(\d[\d,]*)[ \t]*drops?", r"drops?\D{0,20}(\d[\d,]*)")
    items = _first(r"(\d[\d,]*)[ \t]*items?", r"items?\D{0,20}(\d[\d,]*)")
    value = _first(r"(?:value|worth|total)\D{0,20}\$?\s*(\d[\d,]*\.?\d*)",
                   r"\$\s*(\d[\d,]*\.?\d*)")
    return {"drops": drops, "items": items, "value": value, "notes": ""}


def make_row(week, panel, parsed, *, date=None):
    """Build one sheet row (keyed by drop_sheets.COLUMNS) for a panel."""
    parsed = parsed or {}
    return {
        "week": week,
        "date": date or datetime.now().date().isoformat(),
        "panel": panel,
        "drops": parsed.get("drops", ""),
        "items": parsed.get("items", ""),
        "value": parsed.get("value", ""),
        "notes": parsed.get("notes", ""),
    }


def write_buffer(path, week, rows, *, generated=None):
    """Write the weekly buffer (one file per week, overwriting). Returns payload.

    Never raises — a failed write is logged so the caller can still push/report.
    """
    payload = {
        "week": week,
        "generated": generated or datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        log.warning("could not write drop-stats buffer %s: %s", path, exc)
    return payload


def load_buffer(path):
    """Read a weekly buffer back (or ``None`` if missing/unreadable)."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read drop-stats buffer %s: %s", path, exc)
        return None


def format_report(week, rows, push=None):
    """Render ibo's weekly report (skill 5 step 5).

        🐕 Weekly drops — 2026-W23
        • Panel#1 — 312 drops · ~$xx
        • Panel#2 — 280 drops
        Total: 592 · saved to Sheets ✅
    """
    lines = [f"🐕 Weekly drops — {week}"]
    total = 0
    for r in rows:
        drops = str(r.get("drops", "") or "")
        value = str(r.get("value", "") or "")
        try:
            total += int(drops)
        except (TypeError, ValueError):
            pass
        shown = drops or "?"
        val = f" · ~${value}" if value else ""
        lines.append(f"• {r.get('panel', 'Panel#?')} — {shown} drops{val}")

    push = push or {}
    if push.get("ok"):
        tail = f"Total: {total} · saved to Sheets ✅"
    elif push.get("reason") == "not configured":
        tail = f"Total: {total} · buffered, no API key yet"
    else:
        reason = push.get("reason", "")
        tail = f"Total: {total} · buffered ({reason})" if reason else f"Total: {total} · buffered"
    lines.append(tail)
    return "\n".join(lines)


def seconds_until(now, *, weekday=RUN_WEEKDAY, hour=RUN_HOUR, minute=RUN_MINUTE):
    """Seconds from ``now`` to the next ``weekday`` at ``hour:minute`` (strictly
    in the future — exactly on the mark schedules a full week out)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target += timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _bridge_sheets_env(cfg):
    """Mirror the resolved ``GSHEETS_*`` config into the environment so
    drop_sheets.py (which reads os.environ) sees them — without editing it."""
    if cfg.gsheets_credentials:
        os.environ["GSHEETS_CREDENTIALS"] = cfg.gsheets_credentials
    if cfg.gsheets_sheet_id:
        os.environ["GSHEETS_SHEET_ID"] = cfg.gsheets_sheet_id
    if cfg.gsheets_tab:
        os.environ["GSHEETS_TAB"] = cfg.gsheets_tab


def push_to_sheets(cfg, rows):
    """Bridge config -> env, then append the rows. Returns drop_sheets' status."""
    _bridge_sheets_env(cfg)
    return drop_sheets.append_week(rows)


# --- panel driving (Telethon) ----------------------------------------------
def _folder_ref(name):
    """A numeric folder string is an id; otherwise a name."""
    if isinstance(name, str) and name.strip().lstrip("-").isdigit():
        return int(name.strip())
    return name


async def load_panels(client, cfg):
    """Resolve the PANELS folder to ``[(name, entity)]`` (skill 0).

    Returns ``[]`` (logged) if the folder can't be read, so a missing roster
    degrades to an empty — not crashing — run.
    """
    try:
        info = await tg_tools.folder_chats(client, _folder_ref(cfg.panels_folder))
    except Exception as exc:  # noqa: BLE001  (KeyError + API errors)
        log.warning("could not load panels folder %r: %s", cfg.panels_folder, exc)
        return []
    panels = []
    for c in info.get("chats", []):
        try:
            ent = await client.get_entity(c["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("panel resolve failed for %s: %s", c.get("id"), exc)
            continue
        panels.append((c.get("name") or tg_tools.entity_name(ent), ent))
    return panels


async def _await_reply(client, ent, after_id, *, need_buttons=False,
                       timeout=20.0, poll=1.5):
    """Poll for an INCOMING message newer than ``after_id`` (optionally one that
    carries inline buttons). Returns the message or ``None`` on timeout."""
    waited = 0.0
    while True:
        try:
            msgs = await client.get_messages(ent, limit=6)  # newest first
        except Exception as exc:  # noqa: BLE001
            log.warning("read failed for %s: %s", tg_tools.entity_name(ent), exc)
            return None
        for m in msgs:
            if getattr(m, "out", False):
                continue
            if after_id and m.id <= after_id:
                continue
            if need_buttons and not getattr(m, "buttons", None):
                continue
            return m
        if waited >= timeout:
            return None
        await asyncio.sleep(poll)
        waited += poll


async def _press(message, prefixes):
    """Click the first inline button whose label starts with any prefix
    (case-insensitive). Returns True if a button was pressed."""
    rows = getattr(message, "buttons", None) or []
    for row in rows:
        for btn in row:
            label = (getattr(btn, "text", "") or "").strip().lower()
            if any(label.startswith(p) for p in prefixes):
                await message.click(text=btn.text)
                return True
    return False


async def _open_menu(client, ent, *, timeout=20.0):
    """Send ``/start`` and return the bot's reply bearing the inline menu."""
    sent = await client.send_message(ent, "/start")
    return await _await_reply(client, ent, sent.id, need_buttons=True, timeout=timeout)


async def stop_farm(client, ent):
    """Open the panel's menu and press its stop control. True if pressed."""
    menu = await _open_menu(client, ent)
    if menu is None:
        log.warning("%s: no /start menu — cannot stop farm", tg_tools.entity_name(ent))
        return False
    pressed = await _press(menu, STOP_BUTTONS)
    if not pressed:
        log.warning("%s: no stop button found in menu", tg_tools.entity_name(ent))
    return pressed


async def request_drop_stats(client, ent, *, timeout=25.0):
    """Press *Drops Stats* and return the reply text ("" if unavailable)."""
    menu = await _open_menu(client, ent)
    if menu is None:
        return ""
    if not await _press(menu, DROPS_BUTTONS):
        log.warning("%s: no Drops Stats button found", tg_tools.entity_name(ent))
        return ""
    reply = await _await_reply(client, ent, menu.id, timeout=timeout)
    return (reply.message or "") if reply else ""


async def run_activity_booster(client, ent):
    """Open the panel's menu and press *Run activity booster*. True if pressed.

    Operator rule: this runs AFTER Drop Stats for the same panel.
    """
    menu = await _open_menu(client, ent)
    if menu is None:
        log.warning("%s: no /start menu — cannot run activity booster",
                    tg_tools.entity_name(ent))
        return False
    pressed = await _press(menu, BOOSTER_BUTTONS)
    if not pressed:
        log.warning("%s: no activity booster button found", tg_tools.entity_name(ent))
    return pressed


async def collect_week(client, cfg, panels, *, week, date=None, deliver=True):
    """Stop each farm, pull its drops, and return one row per panel.

    One slow/dead panel never blocks the rest — failures are logged and that
    panel still gets a (blank) row marked in its notes. With ``deliver=False``
    (a dry run) NO buttons are pressed — each panel just gets a 'dry-run' row.
    """
    rows = []
    for name, ent in panels:
        panel = panel_label(name)
        text = ""
        if not deliver:
            log.info("[DRY-RUN] %s: would stop farm -> drop stats -> activity booster", panel)
            parsed = parse_drop_stats("")
            parsed["notes"] = "dry-run"
            rows.append(make_row(week, panel, parsed, date=date))
            continue
        try:
            await stop_farm(client, ent)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: stop_farm failed: %s", panel, exc)
        try:
            text = await request_drop_stats(client, ent)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: drop-stats request failed: %s", panel, exc)
        # Operator rule: run the activity booster AFTER drop stats for this panel.
        try:
            await run_activity_booster(client, ent)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: activity booster failed: %s", panel, exc)
        parsed = parse_drop_stats(text)
        if not text.strip():
            parsed["notes"] = "no reply"
        rows.append(make_row(week, panel, parsed, date=date))
        log.info("%s: drops=%s items=%s value=%s",
                 panel, parsed["drops"] or "?", parsed["items"] or "?",
                 parsed["value"] or "?")
    return rows


# --- orchestration + scheduler ---------------------------------------------
async def _send(client, target, text, deliver=True):
    # `target` may be a single entity OR a list/tuple — when it's a list we send
    # to EACH (best-effort: one failure doesn't abort the rest) so the scheduled
    # weekly job reaches every allowed user. Returns True only if all sent.
    if isinstance(target, (list, tuple)):
        if not target:
            return False
        results = [await _send(client, t, text, deliver) for t in target]
        return all(results)
    if not deliver:
        log.info("[DRY-RUN] would send: %s", " ".join((text or "").split())[:160])
        return True
    try:
        await client.send_message(target, (text or "")[:4000])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("send to %s failed: %s", target, exc)
        return False


async def run_weekly(client, cfg, target=None, *, deliver=True, now=None):
    """Run the full skill-5 job once: stop farms, pull stats, buffer, push, report.

    Returns ``{"week", "rows", "push", "path"}``.
    """
    now = now or datetime.now()
    week = iso_week(now)
    log.info("Weekly drop stats — %s", week)

    panels = await load_panels(client, cfg)
    if not panels:
        log.warning("No panels resolved in folder %r; nothing to collect.", cfg.panels_folder)
    rows = await collect_week(client, cfg, panels, week=week, date=now.date().isoformat(),
                              deliver=deliver)

    path = buffer_path(cfg.drop_stats_dir, week)
    write_buffer(path, week, rows, generated=now.isoformat(timespec="seconds"))

    push = push_to_sheets(cfg, rows)
    log.info("Sheets push: %s", push)

    report = format_report(week, rows, push)
    if target is not None:
        await _send(client, target, report, deliver)
    return {"week": week, "rows": rows, "push": push, "path": path}


async def weekly_loop(client, cfg, target=None, *, deliver=True):
    """Sleep until the next Wednesday 00:00, run the job, repeat. Survives a
    failed run (logged) and never double-fires within the trigger minute."""
    while True:
        delay = seconds_until(datetime.now())
        log.info("Next weekly drop-stats run in %.1f h (%s)",
                 delay / 3600.0, "Wed 00:00")
        await asyncio.sleep(delay)
        try:
            await run_weekly(client, cfg, target, deliver=deliver)
        except Exception:  # noqa: BLE001
            log.exception("weekly drop-stats run failed; continuing")
        await asyncio.sleep(60)  # step past the trigger minute before re-arming
