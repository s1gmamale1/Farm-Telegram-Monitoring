"""SQLite-backed incident history. Pure stdlib."""

from __future__ import annotations

import logging
import os
import sqlite3
import time


class IncidentStore:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # check_same_thread=False so the single background loop can reuse it;
        # we only ever touch it from one thread anyway.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                bot         TEXT    NOT NULL,
                severity    TEXT    NOT NULL,
                summary     TEXT,
                root_cause  TEXT,
                fix         TEXT,
                raw_hash    TEXT    NOT NULL,
                raw_excerpt TEXT,
                notified    INTEGER NOT NULL DEFAULT 0,
                benign      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_hash_ts ON incidents(raw_hash, ts)"
        )
        # Migration: pre-existing DBs gain the `benign` flag in place (mirrors the
        # incident_tracker novel-column migration).
        try:
            self.conn.execute(
                "ALTER TABLE incidents ADD COLUMN benign INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                logging.getLogger("watcherdog.storage").warning(
                    "benign-column migration failed: %s", exc)
        self.conn.commit()

    def last_seen(self, raw_hash, notified_only=False):
        """Most recent ts for this error hash, or None. With notified_only, consider
        only rows that were actually alerted (notified=1) — so an un-alerted,
        below-threshold record can't keep a hash 'fresh' and suppress a later real
        alert."""
        sql = "SELECT ts FROM incidents WHERE raw_hash = ?"
        if notified_only:
            sql += " AND notified = 1"
        sql += " ORDER BY ts DESC LIMIT 1"
        cur = self.conn.execute(sql, (raw_hash,))
        row = cur.fetchone()
        return row["ts"] if row else None

    def record(self, bot, severity, analysis, raw_hash, raw_excerpt, notified,
               ts=None, benign=False):
        ts = ts if ts is not None else time.time()
        cur = self.conn.execute(
            """
            INSERT INTO incidents
                (ts, bot, severity, summary, root_cause, fix, raw_hash, raw_excerpt,
                 notified, benign)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                bot,
                severity,
                (analysis or {}).get("summary", ""),
                (analysis or {}).get("root_cause", ""),
                (analysis or {}).get("fix", ""),
                raw_hash,
                raw_excerpt[:4000],
                1 if notified else 0,
                1 if benign else 0,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def recurring(self, window_seconds, min_count, now=None):
        """Errors whose identical hash has recurred at least ``min_count`` times
        within the trailing ``window_seconds``.

        Returns a list of group dicts, most frequent first::

            {"raw_hash", "count", "last_ts", "bots": [..],
             "severity", "summary", "raw_excerpt"}

        ``severity``/``summary``/``raw_excerpt`` come from the latest matching
        incident (what to show in the alert).
        """
        now = now if now is not None else time.time()
        since = now - window_seconds
        cur = self.conn.execute(
            """
            SELECT raw_hash,
                   COUNT(*)                   AS count,
                   MAX(ts)                    AS last_ts,
                   GROUP_CONCAT(DISTINCT bot) AS bots
            FROM incidents
            WHERE ts >= ? AND benign = 0
            GROUP BY raw_hash
            HAVING COUNT(*) >= ?
            ORDER BY count DESC, last_ts DESC
            """,
            (since, min_count),
        )
        groups = []
        for row in cur.fetchall():
            latest = self.conn.execute(
                "SELECT severity, summary, raw_excerpt FROM incidents "
                "WHERE raw_hash = ? AND benign = 0 ORDER BY ts DESC LIMIT 1",
                (row["raw_hash"],),
            ).fetchone()
            groups.append({
                "raw_hash": row["raw_hash"],
                "count": row["count"],
                "last_ts": row["last_ts"],
                "bots": [b for b in (row["bots"] or "").split(",") if b],
                "severity": latest["severity"] if latest else "",
                "summary": latest["summary"] if latest else "",
                "raw_excerpt": latest["raw_excerpt"] if latest else "",
            })
        return groups

    def recent(self, limit=20):
        cur = self.conn.execute(
            "SELECT * FROM incidents ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
