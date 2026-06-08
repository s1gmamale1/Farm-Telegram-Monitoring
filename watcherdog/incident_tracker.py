"""SQLite-backed open-incident lifecycle ledger. Pure stdlib.

Layered on top of the append-only ``incidents`` history (storage.IncidentStore):
each proactive alert path OPENS an incident here; it is RESOLVED (event-driven)
when the bot/panel goes healthy again, or ESCALATED by the follow-up loop after a
give-up window. Logic is I/O-free apart from SQLite and takes an injectable
``now`` so it unit-tests deterministically. Uses its own connection to the same
DB file as IncidentStore; both are touched only from the single monitor thread.
See docs/superpowers/specs/2026-06-09-incident-lifecycle-tracking-design.md.
"""

from __future__ import annotations

import os
import sqlite3
import time


class IncidentTracker:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS open_incidents (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                key            TEXT    NOT NULL,
                source         TEXT    NOT NULL,
                bot            TEXT    NOT NULL,
                severity       TEXT,
                summary        TEXT,
                raw_excerpt    TEXT,
                fixable        INTEGER NOT NULL DEFAULT 0,
                fix_attempted  TEXT,
                fix_retries    INTEGER NOT NULL DEFAULT 0,
                opened_ts      REAL    NOT NULL,
                last_update_ts REAL    NOT NULL,
                update_count   INTEGER NOT NULL DEFAULT 0,
                status         TEXT    NOT NULL DEFAULT 'open',
                resolved_ts    REAL,
                resolution     TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_open_incidents_status "
            "ON open_incidents(status, key)"
        )
        self.conn.commit()

    # --- internal lookups ---------------------------------------------------
    def _open_by_key(self, key):
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE key = ? AND status = 'open' "
            "ORDER BY opened_ts DESC LIMIT 1",
            (key,),
        ).fetchone()

    def open_for_bot(self, source, bot):
        """Most-recent OPEN incident for a (source, bot), or None."""
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE source = ? AND bot = ? "
            "AND status = 'open' ORDER BY opened_ts DESC LIMIT 1",
            (source, bot),
        ).fetchone()

    # --- mutations ----------------------------------------------------------
    def open(self, source, bot, key, severity, summary, *, fixable,
             fix_attempted=None, raw_excerpt=None, now=None):
        """Open an incident. Idempotent: if one is already open for ``key`` the
        existing row is returned unchanged. Returns the open row."""
        now = now if now is not None else time.time()
        existing = self._open_by_key(key)
        if existing is not None:
            return existing
        self.conn.execute(
            """
            INSERT INTO open_incidents
                (key, source, bot, severity, summary, raw_excerpt, fixable,
                 fix_attempted, fix_retries, opened_ts, last_update_ts,
                 update_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'open')
            """,
            (key, source, bot, severity, summary, raw_excerpt,
             1 if fixable else 0, fix_attempted, now, now),
        )
        self.conn.commit()
        return self._open_by_key(key)

    def resolve_by_bot(self, source, bot, resolution, now=None):
        """Resolve the most-recent open incident for a (source, bot). Returns
        ``{"elapsed", "fix_attempted", "row"}`` or None if nothing was open. The
        original error's hash isn't known at heal time, so resolution is keyed by
        (source, bot) rather than the full open() key."""
        now = now if now is not None else time.time()
        row = self.open_for_bot(source, bot)
        if row is None:
            return None
        self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE id = ?",
            (now, resolution, row["id"]),
        )
        self.conn.commit()
        return {
            "elapsed": now - row["opened_ts"],
            "fix_attempted": row["fix_attempted"],
            "row": dict(row),
        }

    def note_fix_attempt(self, key, fix_attempted):
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET fix_attempted = ?, "
            "fix_retries = fix_retries + 1 WHERE id = ?",
            (fix_attempted, row["id"]),
        )
        self.conn.commit()

    def mark_followed_up(self, key, now=None):
        now = now if now is not None else time.time()
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET last_update_ts = ?, "
            "update_count = update_count + 1 WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()

    def escalate(self, key, now=None):
        now = now if now is not None else time.time()
        row = self._open_by_key(key)
        if row is None:
            return
        self.conn.execute(
            "UPDATE open_incidents SET status = 'escalated', resolved_ts = ?, "
            "resolution = 'gave_up' WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()

    # --- queries ------------------------------------------------------------
    def open_list(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' ORDER BY opened_ts"
        ).fetchall()]

    def due_for_followup(self, interval_s, now):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' "
            "AND (? - last_update_ts) >= ? ORDER BY opened_ts",
            (now, interval_s),
        ).fetchall()]

    def due_for_giveup(self, giveup_s, now):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' "
            "AND (? - opened_ts) >= ? ORDER BY opened_ts",
            (now, giveup_s),
        ).fetchall()]

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def incident_followup_step(tracker, now, *, followup_interval_s, giveup_s,
                           max_fix_retries):
    """Pure planner: decide what the follow-up loop should DO this tick.

    Returns a list of action dicts ``{"kind", "row"}`` for the async loop to
    execute. Kinds:
      * ``giveup``   — past the give-up window: escalate + final message.
      * ``refix``    — open, fixable, source ``bot_error``, retry budget left:
                       re-run the known fix, then nag.
      * ``followup`` — otherwise: nag only.
    Give-up (keyed off ``opened_ts``) wins over follow-up for the same incident.
    """
    actions = []
    giveup_ids = set()
    for row in tracker.due_for_giveup(giveup_s, now):
        actions.append({"kind": "giveup", "row": row})
        giveup_ids.add(row["id"])
    for row in tracker.due_for_followup(followup_interval_s, now):
        if row["id"] in giveup_ids:
            continue
        if (row["fixable"] and row["source"] == "bot_error"
                and row["fix_retries"] < max_fix_retries):
            actions.append({"kind": "refix", "row": row})
        else:
            actions.append({"kind": "followup", "row": row})
    return actions
