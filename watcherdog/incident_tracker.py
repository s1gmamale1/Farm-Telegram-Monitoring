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

import logging
import os
import sqlite3
import time


class IncidentTracker:
    def __init__(self, db_path, *, dry_run=False):
        """dry_run=True makes all mutating methods no-op (queries stay live) so a
        rehearsal never writes the real ledger."""
        self._dry_run = dry_run
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # This is a SECOND connection to the same DB file as storage.IncidentStore.
        # Both are driven from the single monitor-loop thread (no method holds a
        # txn across an await), so contention is not expected — but wait rather
        # than fail instantly if the two ever overlap on commit.
        self.conn.execute("PRAGMA busy_timeout=5000")
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
                novel          INTEGER NOT NULL DEFAULT 0,
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
        # Phase 4 migration: pre-existing DBs gain the `novel` flag in place.
        try:
            self.conn.execute(
                "ALTER TABLE open_incidents ADD COLUMN novel INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            # Duplicate column = already migrated (the normal case). Anything
            # else (e.g. database is locked) degrades safely — open()/novel_list
            # tolerate a missing column — but is worth a log line.
            if "duplicate column" not in str(exc).lower():
                logging.getLogger("watcherdog.incident_tracker").warning(
                    "novel-column migration failed: %s", exc)
        self.conn.commit()

    # --- internal lookups ---------------------------------------------------
    def _open_by_key(self, key):
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE key = ? AND status = 'open' "
            "ORDER BY opened_ts DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()

    def open_for_bot(self, source, bot):
        """Most-recent OPEN incident for a (source, bot), or None. The ``id DESC``
        tiebreaker makes the pick deterministic when two rows share ``opened_ts``."""
        return self.conn.execute(
            "SELECT * FROM open_incidents WHERE source = ? AND bot = ? "
            "AND status = 'open' ORDER BY opened_ts DESC, id DESC LIMIT 1",
            (source, bot),
        ).fetchone()

    # --- mutations ----------------------------------------------------------
    def open(self, source, bot, key, severity, summary, *, fixable,
             novel=False, fix_attempted=None, raw_excerpt=None, now=None):
        """Open an incident. Idempotent: if one is already open for ``key`` the
        existing row is returned unchanged. Returns the open row. ``novel=True``
        flags an error with no learned fix (Phase 4) — the overseer queue."""
        if self._dry_run:
            return None
        now = now if now is not None else time.time()
        existing = self._open_by_key(key)
        if existing is not None:
            return existing
        try:
            self.conn.execute(
                """
                INSERT INTO open_incidents
                    (key, source, bot, severity, summary, raw_excerpt, fixable,
                     novel, fix_attempted, fix_retries, opened_ts, last_update_ts,
                     update_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, 'open')
                """,
                (key, source, bot, severity, summary, raw_excerpt,
                 1 if fixable else 0, 1 if novel else 0, fix_attempted, now, now),
            )
        except sqlite3.OperationalError:
            # `novel` column absent (migration failed on an exotic DB): degrade to
            # the legacy insert rather than crash the watcher.
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

    def refresh(self, key, severity, summary, *, raw_excerpt=None, fixable=None):
        """Update an OPEN incident's severity/summary/excerpt in place (a worse or
        distinct error superseded the original). No-op if the key isn't open.

        ``open()`` is idempotent — it returns an already-open row UNCHANGED — so
        this is the path the suppression gate uses to record that the open
        incident's nature changed (new error hash, or risen severity). Returns the
        refreshed row, or None when nothing is open for ``key``."""
        if self._dry_run:
            return None
        row = self._open_by_key(key)
        if row is None:
            return None
        sets = ["severity = ?", "summary = ?"]
        vals = [severity, summary]
        if raw_excerpt is not None:
            sets.append("raw_excerpt = ?")
            vals.append(raw_excerpt)
        if fixable is not None:
            sets.append("fixable = ?")
            vals.append(1 if fixable else 0)
        vals.append(row["id"])
        self.conn.execute(
            f"UPDATE open_incidents SET {', '.join(sets)} WHERE id = ?", vals
        )
        self.conn.commit()
        return self._open_by_key(key)

    def resolve_by_bot(self, source, bot, resolution, now=None):
        """Resolve the most-recent open incident for a (source, bot). Returns
        ``{"elapsed", "fix_attempted", "row"}`` or None if nothing was open. The
        original error's hash isn't known at heal time, so resolution is keyed by
        (source, bot) rather than the full open() key."""
        if self._dry_run:
            return None
        now = now if now is not None else time.time()
        row = self.open_for_bot(source, bot)
        if row is None:
            return None
        self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE id = ? AND status = 'open'",
            (now, resolution, row["id"]),
        )
        self.conn.commit()
        return {
            "elapsed": now - row["opened_ts"],
            "fix_attempted": row["fix_attempted"],
            "row": dict(row),
        }

    def open_list_for_bot(self, bot):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE bot = ? AND status = 'open' "
            "ORDER BY opened_ts", (bot,)).fetchall()]

    def resolve_open_for_bot(self, bot, resolution, now=None):
        """Resolve ALL open incidents for a bot regardless of source (the bot/panel
        is healthy again — close everything). Returns {elapsed (from earliest
        opened_ts), we_fixed (any attempt reported 'fixed'), count} or None."""
        if self._dry_run:
            return None
        now = now if now is not None else time.time()
        rows = self.open_list_for_bot(bot)
        if not rows:
            return None
        earliest = min(r["opened_ts"] for r in rows)
        we_fixed = any((r["fix_attempted"] == "fixed") for r in rows)
        # Preserve the we_fixed/self_healed vocabulary in the stored column: a
        # successful re-fix on any row wins; otherwise store the caller's base.
        stored = "we_fixed" if we_fixed else resolution
        self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE bot = ? AND status = 'open'",
            (now, stored, bot))
        self.conn.commit()
        return {"elapsed": now - earliest, "we_fixed": we_fixed, "count": len(rows)}

    def note_fix_attempt(self, key, fix_attempted):
        if self._dry_run:
            return
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
        if self._dry_run:
            return
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
        if self._dry_run:
            return
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

    # --- by-id mutations (race-safe) ---------------------------------------
    # The follow-up tick snapshots a row, AWAITS (network/buttons), then mutates.
    # During the await a monitor sweep can resolve+REOPEN the same key, so a
    # KEY-based mutation would land on the NEW row (pre-burning its budget /
    # sending a stale alert). These target the snapshotted ROW ID instead; the
    # ``AND status = 'open'`` guard makes a mutation on an already-resolved id a
    # safe NO-OP (the new row's budget is left untouched).
    def get_open_by_id(self, incident_id):
        """The OPEN row with this id, or None (resolved/escalated/unknown all None)."""
        row = self.conn.execute(
            "SELECT * FROM open_incidents WHERE id = ? AND status = 'open'",
            (incident_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def note_fix_attempt_by_id(self, incident_id, fix_attempted):
        if self._dry_run:
            return
        self.conn.execute(
            "UPDATE open_incidents SET fix_attempted = ?, "
            "fix_retries = fix_retries + 1 WHERE id = ? AND status = 'open'",
            (fix_attempted, incident_id),
        )
        self.conn.commit()

    def mark_followed_up_by_id(self, incident_id, now=None):
        if self._dry_run:
            return
        now = now if now is not None else time.time()
        self.conn.execute(
            "UPDATE open_incidents SET last_update_ts = ?, "
            "update_count = update_count + 1 WHERE id = ? AND status = 'open'",
            (now, incident_id),
        )
        self.conn.commit()

    def escalate_by_id(self, incident_id, now=None):
        if self._dry_run:
            return
        now = now if now is not None else time.time()
        self.conn.execute(
            "UPDATE open_incidents SET status = 'escalated', resolved_ts = ?, "
            "resolution = 'gave_up' WHERE id = ? AND status = 'open'",
            (now, incident_id),
        )
        self.conn.commit()

    def resolve_by_id(self, incident_id, resolution, now=None):
        """Resolve ONE open row by id (the overseer's resolve_flagged). Returns
        True iff a row was actually closed; a resolved/unknown id is False."""
        if self._dry_run:
            return False
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "UPDATE open_incidents SET status = 'resolved', resolved_ts = ?, "
            "resolution = ? WHERE id = ? AND status = 'open'",
            (now, resolution, incident_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # --- queries ------------------------------------------------------------
    def open_list(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM open_incidents WHERE status = 'open' ORDER BY opened_ts"
        ).fetchall()]

    def novel_list(self):
        """Open incidents flagged novel (the Phase 5 overseer queue), oldest first."""
        try:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM open_incidents WHERE status = 'open' AND novel = 1 "
                "ORDER BY opened_ts").fetchall()]
        except sqlite3.OperationalError:
            return []

    def escalated_list(self, since=None):
        """Incidents that ESCALATED (auto-recovery gave up → human alerted, so they
        drop out of the open queue). With ``since``, only those whose ``resolved_ts``
        (the escalation time) is >= ``since``. Newest first. Lets the overseer still
        SEE a needs-human panel that is no longer 'open'."""
        try:
            if since is None:
                rows = self.conn.execute(
                    "SELECT * FROM open_incidents WHERE status = 'escalated' "
                    "ORDER BY resolved_ts DESC").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM open_incidents WHERE status = 'escalated' "
                    "AND resolved_ts >= ? ORDER BY resolved_ts DESC", (since,)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

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
