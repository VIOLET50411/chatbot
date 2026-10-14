"""宿主桥接的持久入站队列；核心或桥接重启时不丢已捕获消息。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class HostInboundSpool:
    def __init__(self, database_path: str | Path, *, maximum_pending: int = 5_000) -> None:
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.maximum_pending = max(100, min(int(maximum_pending), 50_000))
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbound_spool (
                    source_fingerprint TEXT PRIMARY KEY,
                    external_contact_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text_content TEXT,
                    file_path TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'failed')),
                    error_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_spool_status_created
                ON inbound_spool(status, created_at);
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(inbound_spool)").fetchall()
            }
            if "context_mode" not in columns:
                connection.execute(
                    "ALTER TABLE inbound_spool ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'live'"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        """sqlite3 的上下文管理器只提交事务，不会关闭句柄；这里确保 Windows 能立即重启和清理。"""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def put(self, item: object) -> bool:
        with self._lock, self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM inbound_spool WHERE status='pending'").fetchone()[0])
            if count >= self.maximum_pending:
                raise OverflowError("宿主持久入站队列已满")
            now = self._now()
            result = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_spool(
                    source_fingerprint, external_contact_id, occurred_at, message_type,
                    text_content, file_path, attempts, status, created_at, updated_at, context_mode
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?, ?)
                """,
                (
                    str(getattr(item, "source_fingerprint")),
                    str(getattr(item, "external_contact_id")),
                    getattr(item, "occurred_at").isoformat(),
                    str(getattr(item, "message_type")),
                    getattr(item, "text"),
                    str(getattr(item, "file_path")) if getattr(item, "file_path") is not None else None,
                    now,
                    now,
                    str(getattr(item, "context_mode", "live")),
                ),
            )
            return result.rowcount == 1

    def next(self, item_type: type) -> object | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM inbound_spool WHERE status='pending'
                ORDER BY created_at, source_fingerprint LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        occurred_at = datetime.fromisoformat(str(row["occurred_at"]))
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        return item_type(
            external_contact_id=str(row["external_contact_id"]),
            occurred_at=occurred_at,
            message_type=str(row["message_type"]),
            text=str(row["text_content"]) if row["text_content"] is not None else None,
            file_path=Path(str(row["file_path"])) if row["file_path"] else None,
            source_fingerprint=str(row["source_fingerprint"]),
            attempts=int(row["attempts"]),
            context_mode=str(row["context_mode"] or "live"),
        )

    def complete(self, source_fingerprint: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM inbound_spool WHERE source_fingerprint=?", (source_fingerprint,))

    def fail(self, source_fingerprint: str, error: BaseException, *, maximum_attempts: int = 4) -> bool:
        summary = type(error).__name__[:200]
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM inbound_spool WHERE source_fingerprint=? AND status='pending'",
                (source_fingerprint,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + 1
            terminal = attempts >= max(1, int(maximum_attempts))
            connection.execute(
                """
                UPDATE inbound_spool SET attempts=?, status=?, error_summary=?, updated_at=?
                WHERE source_fingerprint=?
                """,
                (attempts, "failed" if terminal else "pending", summary, self._now(), source_fingerprint),
            )
            return not terminal

    def counts(self) -> dict[str, int]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM inbound_spool GROUP BY status").fetchall()
        counts = {"pending": 0, "failed": 0}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return counts
