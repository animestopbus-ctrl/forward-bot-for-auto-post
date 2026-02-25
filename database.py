"""
database.py  –  SQLite persistence layer
════════════════════════════════════════

Tables
──────
settings    key/value store for all runtime bot settings
post_log    record of every message successfully published
admins      extra admin user IDs (beyond the config list)
"""

import sqlite3
import threading
from typing import Any, Optional


class Database:
    """
    Thread-safe SQLite wrapper.  All public methods acquire a reentrant lock
    so the async PTB threads can safely call them from synchronous contexts.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id  INTEGER NOT NULL,
                    source_msg_id   INTEGER NOT NULL,
                    target_chat_id  INTEGER NOT NULL,
                    target_msg_id   INTEGER,
                    filename        TEXT,
                    posted_at       DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS filters (
                    keyword TEXT PRIMARY KEY
                );
            """)
            self._conn.commit()

    # ── Settings ──────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()

    # ── Helpers for typed settings ────────────────────────────────────────────

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        if v is None:
            return default
        return v.lower() in ("1", "true", "yes")

    # ── Post log ──────────────────────────────────────────────────────────────

    def log_post(
        self,
        source_chat_id: int,
        source_msg_id: int,
        target_chat_id: int,
        target_msg_id: Optional[int],
        filename: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO post_log
                   (source_chat_id, source_msg_id, target_chat_id, target_msg_id, filename)
                   VALUES (?, ?, ?, ?, ?)""",
                (source_chat_id, source_msg_id, target_chat_id, target_msg_id, filename),
            )
            self._conn.commit()

    def total_posted(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) as n FROM post_log").fetchone()
            return row["n"] if row else 0

    def last_post_time(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT posted_at FROM post_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["posted_at"] if row else None

    def was_posted(self, source_chat_id: int, source_msg_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM post_log WHERE source_chat_id=? AND source_msg_id=?",
                (source_chat_id, source_msg_id),
            ).fetchone()
            return row is not None

    # ── Extra admins ──────────────────────────────────────────────────────────

    def add_admin(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,)
            )
            self._conn.commit()

    def remove_admin(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def extra_admins(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute("SELECT user_id FROM admins").fetchall()
            return [r["user_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
