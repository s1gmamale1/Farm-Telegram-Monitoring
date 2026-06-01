"""SQLite-backed incident history. Pure stdlib."""

from __future__ import annotations

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
                notified    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_hash_ts ON incidents(raw_hash, ts)"
        )
        self.conn.commit()

    def last_seen(self, raw_hash):
        """Return the most recent timestamp for this error hash, or None."""
        cur = self.conn.execute(
            "SELECT ts FROM incidents WHERE raw_hash = ? ORDER BY ts DESC LIMIT 1",
            (raw_hash,),
        )
        row = cur.fetchone()
        return row["ts"] if row else None

    def record(self, bot, severity, analysis, raw_hash, raw_excerpt, notified, ts=None):
        ts = ts if ts is not None else time.time()
        cur = self.conn.execute(
            """
            INSERT INTO incidents
                (ts, bot, severity, summary, root_cause, fix, raw_hash, raw_excerpt, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self.conn.commit()
        return cur.lastrowid

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
