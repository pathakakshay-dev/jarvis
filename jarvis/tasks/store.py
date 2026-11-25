"""SQLite persistence for reminders. Startup never executes actions."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class ReminderStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recurrence TEXT NOT NULL,
                    recurrence_until TEXT,
                    snooze_until TEXT,
                    last_delivered_for TEXT,
                    missed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_status_due ON reminders(status, due_at);
                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    reminder_id TEXT NOT NULL,
                    occurrence TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (reminder_id, occurrence)
                );
                """
            )
            self._conn.commit()

    def upsert(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reminders (
                    id, title, notes, timezone, due_at, status, recurrence, recurrence_until,
                    snooze_until, last_delivered_for, missed, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    notes=excluded.notes,
                    timezone=excluded.timezone,
                    due_at=excluded.due_at,
                    status=excluded.status,
                    recurrence=excluded.recurrence,
                    recurrence_until=excluded.recurrence_until,
                    snooze_until=excluded.snooze_until,
                    last_delivered_for=excluded.last_delivered_for,
                    missed=excluded.missed,
                    updated_at=excluded.updated_at,
                    completed_at=excluded.completed_at
                """,
                (
                    row["id"],
                    row["title"],
                    row.get("notes") or "",
                    row["timezone"],
                    row["due_at"],
                    row["status"],
                    row["recurrence"],
                    row.get("recurrence_until"),
                    row.get("snooze_until"),
                    row.get("last_delivered_for"),
                    1 if row.get("missed") else 0,
                    row["created_at"],
                    row["updated_at"],
                    row.get("completed_at"),
                ),
            )
            self._conn.commit()

    def get(self, reminder_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return _row(row)

    def delete(self, reminder_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            self._conn.execute("DELETE FROM reminder_deliveries WHERE reminder_id = ?", (reminder_id,))
            self._conn.commit()

    def list_active(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM reminders
                WHERE status IN ('pending', 'due', 'snoozed')
                ORDER BY due_at ASC
                """
            ).fetchall()
            return [_row(row) for row in rows if row is not None]

    def list_all(self, *, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders ORDER BY due_at ASC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [_row(row) for row in rows if row is not None]

    def record_delivery(self, reminder_id: str, occurrence: str, delivered_at: str, kind: str) -> bool:
        """Return True if this occurrence was newly recorded (not a duplicate)."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO reminder_deliveries (reminder_id, occurrence, delivered_at, kind)
                    VALUES (?, ?, ?, ?)
                    """,
                    (reminder_id, occurrence, delivered_at, kind),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def has_delivery(self, reminder_id: str, occurrence: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM reminder_deliveries WHERE reminder_id = ? AND occurrence = ?",
                (reminder_id, occurrence),
            ).fetchone()
            return row is not None


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "notes": row["notes"] or "",
        "timezone": row["timezone"],
        "due_at": row["due_at"],
        "status": row["status"],
        "recurrence": row["recurrence"],
        "recurrence_until": row["recurrence_until"],
        "snooze_until": row["snooze_until"],
        "last_delivered_for": row["last_delivered_for"],
        "missed": bool(row["missed"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }
