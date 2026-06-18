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
import logging
import os
from datetime import datetime, timedelta

from watcherdog import drop_sheets, tg_tools
from watcherdog.drop_stats_format import (  # re-exported pure helpers (back-compat)
    _report_to_row,
    buffer_path,
    format_report,
    iso_week,
    load_buffer,
    make_row,
    panel_label,
    write_buffer,
)

log = logging.getLogger("watcherdog.drop_stats")

# Button-label prefixes (labels are truncated in Telegram — match by prefix).
STOP_BUTTONS = ("kill all cs", "kill all", "stop the farm", "stop farm", "stop")
DROPS_BUTTONS = ("drops stats", "drop stats", "drops")
# Operator rule: the activity booster must run AFTER drop stats are pulled.
BOOSTER_BUTTONS = ("run activity booster", "activity booster")
# Operator weekly maintenance: collect "purple" accounts before pulling stats.
PURPLE_BUTTONS = ("collect purple", "purple")

# Wednesday = weekday() 2 (Mon=0). Run at 00:00.
RUN_WEEKDAY = 2
RUN_HOUR = 0
RUN_MINUTE = 0


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


async def _await_reply(client, ent, after_id, *, need_buttons=False, match=None,
                       timeout=20.0, poll=1.5):
    """Poll for an INCOMING message newer than ``after_id`` (optionally one that
    carries inline buttons, and/or one matching ``match(m)``). Returns the
    message or ``None`` on timeout."""
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
            if match is not None and not match(m):
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


def _is_terminal_drop_reply(m):
    """Stop waiting on a Drop Stats reply that is terminal: the DROP REPORT
    itself, or a 'can't get drop' failure notice — so a failing panel doesn't
    burn the whole timeout, and its error still reaches the buffer/report."""
    t = (getattr(m, "message", "") or "").lower()
    return "drop report" in t or "can't get drop" in t or "cant get drop" in t


async def request_drop_stats(client, ent, *, timeout=25.0, wait_for_report=False):
    """Press *Drops Stats* and return the reply text ("" if unavailable).

    With ``wait_for_report=True`` it waits (up to ``timeout``) for the panel's
    terminal Drop Stats reply — the DROP REPORT message, or a "can't get drop"
    failure notice — instead of the first reply (see :func:`_is_terminal_drop_reply`).
    Used by the scheduled weekly maintenance run, whose report can take minutes to
    arrive; a failing panel returns its error promptly rather than timing out.
    """
    menu = await _open_menu(client, ent)
    if menu is None:
        return ""
    if not await _press(menu, DROPS_BUTTONS):
        log.warning("%s: no Drops Stats button found", tg_tools.entity_name(ent))
        return ""
    match = _is_terminal_drop_reply if wait_for_report else None
    reply = await _await_reply(client, ent, menu.id, timeout=timeout, match=match)
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


async def collect_purple(client, ent):
    """Open the panel's menu and press *Collect purple accounts*. True if pressed."""
    menu = await _open_menu(client, ent)
    if menu is None:
        log.warning("%s: no /start menu — cannot collect purple", tg_tools.entity_name(ent))
        return False
    pressed = await _press(menu, PURPLE_BUTTONS)
    if not pressed:
        log.warning("%s: no collect-purple button found", tg_tools.entity_name(ent))
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
            parsed = _report_to_row("")
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
        parsed = _report_to_row(text)
        if not text.strip():
            parsed["notes"] = "no reply"
        rows.append(make_row(week, panel, parsed, date=date))
        # Only a genuinely-unread "" renders "?"; a real 0 (e.g. cases=0 or the
        # usual items=0 with no ≥$0.6 skins) must show as 0, not "?".
        _shown = lambda v: "?" if v == "" else v  # noqa: E731
        log.info("%s: cases=%s items=%s value=%s", panel,
                 _shown(parsed["drops"]), _shown(parsed["items"]),
                 _shown(parsed["value"]))
    return rows


async def _for_each_panel(client, panels, action, label):
    """Run ``action(client, ent)`` on every panel; log (never raise) per-panel
    failures so one slow/dead panel never blocks the rest of the phase."""
    for name, ent in panels:
        try:
            await action(client, ent)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: %s failed: %s", panel_label(name), label, exc)


async def collect_maintenance(client, cfg, panels, *, week, date=None, deliver=True):
    """Weekly maintenance collector (global phases): kill ALL farms -> collect
    purple on ALL -> wait ``purple_collect_wait_seconds`` -> Drop Stats on ALL
    (awaiting each panel's terminal Drop Stats reply) -> activity booster on ALL.
    Returns one row per panel. ``deliver=False`` presses nothing and does not sleep.

    The kill/purple/booster phases run via :func:`_for_each_panel`, so one
    slow/dead panel never blocks the rest. The Drop Stats phase is serial (it
    needs each call's reply text) and is bounded per panel by
    ``drop_report_timeout_seconds``."""
    if not deliver:
        log.info("[DRY-RUN] weekly maintenance over %d panels: "
                 "kill->purple->wait->drop->booster", len(panels))
        rows = []
        for name, ent in panels:
            parsed = _report_to_row("")
            parsed["notes"] = "dry-run"
            rows.append(make_row(week, panel_label(name), parsed, date=date))
        return rows

    await _for_each_panel(client, panels, stop_farm, "kill all")
    await _for_each_panel(client, panels, collect_purple, "collect purple")
    await asyncio.sleep(cfg.purple_collect_wait_seconds)

    # Drop phase is serial (not _for_each_panel): we need each call's RETURN value
    # (the reply text) to build that panel's row.
    rows = []
    _shown = lambda v: "?" if v == "" else v  # noqa: E731
    for name, ent in panels:
        panel = panel_label(name)
        text = ""
        try:
            text = await request_drop_stats(
                client, ent, wait_for_report=True,
                timeout=cfg.drop_report_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: drop-stats request failed: %s", panel, exc)
        parsed = _report_to_row(text)
        if not text.strip():
            parsed["notes"] = "no report"            # waited for a DROP REPORT, none arrived
        elif "drop report" not in text.lower() and not parsed["notes"]:
            # terminal non-report reply (e.g. "Can't get drop on N accounts")
            parsed["notes"] = " ".join(text.split())[:200]
        rows.append(make_row(week, panel, parsed, date=date))
        log.info("%s: cases=%s items=%s value=%s", panel,
                 _shown(parsed["drops"]), _shown(parsed["items"]), _shown(parsed["value"]))

    await _for_each_panel(client, panels, run_activity_booster, "activity booster")
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


async def run_weekly(client, cfg, target=None, *, deliver=True, now=None, collector=None):
    """Run the full skill-5 job once: stop farms, pull stats, buffer, push, report.

    ``collector`` selects the collect step (defaults to :func:`collect_week`, the
    fast on-demand pull); ``weekly_loop`` passes :func:`collect_maintenance` for
    the scheduled phased run. Returns ``{"week", "rows", "push", "path", "ok"}``
    (and ``"reason"`` on a failure such as a 0-panel folder resolution).
    """
    now = now or datetime.now()
    week = iso_week(now)
    log.info("Weekly drop stats — %s", week)

    panels = await load_panels(client, cfg)
    if not panels:
        log.warning("No panels resolved in folder %r — NOT collecting (would clobber the "
                    "week buffer with empty data); alerting + leaving last good data.",
                    cfg.panels_folder)
        msg = (f"⚠️ Weekly drop-stats ({week}): could not read the '{cfg.panels_folder}' "
               "folder (0 panels). Skipped — last week's buffer is preserved. "
               "Check the folder name / connection and re-run.")
        if target is not None:
            await _send(client, target, msg, deliver)
        return {"week": week, "ok": False, "reason": "no panels",
                "rows": None, "push": None, "path": None}

    collect = collector or collect_week
    rows = await collect(client, cfg, panels, week=week, date=now.date().isoformat(),
                         deliver=deliver)

    path = buffer_path(cfg.drop_stats_dir, week)
    write_buffer(path, week, rows, generated=now.isoformat(timespec="seconds"))

    push = push_to_sheets(cfg, rows)
    log.info("Sheets push: %s", push)

    report = format_report(week, rows, push)
    if target is not None:
        await _send(client, target, report, deliver)
    return {"week": week, "ok": True, "rows": rows, "push": push, "path": path}


async def weekly_loop(client, cfg, target=None, *, deliver=True):
    """Sleep until the next Wednesday 00:00, run the job, repeat. On a transient
    failure (e.g. a 0-panel folder hiccup or an exception) it retries in 1h
    instead of losing the whole week; only a SUCCESS re-arms for next Wednesday.
    Never double-fires within the trigger minute.

    Alert fatigue guard: a persistent failure streak alerts the owner ONCE — the
    first failed run carries ``target``; every subsequent hourly retry passes
    ``target=None`` so ``run_weekly`` retries SILENTLY. A fresh weekly window (or
    a recovery + new failure) re-arms the alert.

    The scheduled run uses the phased :func:`collect_maintenance` collector when
    ``cfg.weekly_maintenance_enabled`` (default), else the legacy
    :func:`collect_week`. The on-demand "drops stats" command stays a fast pull —
    it calls ``run_weekly`` directly with the default collector."""
    collector = collect_maintenance if getattr(cfg, "weekly_maintenance_enabled", True) else collect_week
    while True:
        delay = seconds_until(datetime.now())
        log.info("Next weekly drop-stats run in %.1f h (%s)",
                 delay / 3600.0, "Wed 00:00")
        await asyncio.sleep(delay)
        # Run, retrying hourly until it succeeds, THEN wait for next Wednesday.
        alerted = False
        while True:
            try:
                res = await run_weekly(client, cfg,
                                       target if not alerted else None,  # alert once per streak
                                       deliver=deliver, collector=collector)
            except Exception:  # noqa: BLE001
                log.exception("weekly drop-stats run failed; retry in 1h")
                res = {"ok": False, "reason": "exception"}
            if res.get("ok"):
                break
            alerted = True  # stay quiet on subsequent hourly retries
            await asyncio.sleep(3600)  # transient (folder/conn) — retry in 1h, not a week
        await asyncio.sleep(60)  # success: step past the trigger minute before re-arming
