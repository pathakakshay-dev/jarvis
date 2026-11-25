"""SQLite checkpoint store for interrupted workflow recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_ACTIVE_RECOVERY = frozenset({"active", "interrupted", "needs_review"})


class CheckpointStore:
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
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recovery_status TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    step_count INTEGER NOT NULL,
                    approval_pending INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    restart_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workflow_checkpoints_recovery
                    ON workflow_checkpoints(recovery_status, kind);
                """
            )
            self._conn.commit()

    def upsert(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workflow_checkpoints (
                    checkpoint_id, execution_id, kind, status, recovery_status,
                    step_index, step_count, approval_pending, summary, restart_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    execution_id=excluded.execution_id,
                    kind=excluded.kind,
                    status=excluded.status,
                    recovery_status=excluded.recovery_status,
                    step_index=excluded.step_index,
                    step_count=excluded.step_count,
                    approval_pending=excluded.approval_pending,
                    summary=excluded.summary,
                    restart_payload=excluded.restart_payload,
                    updated_at=excluded.updated_at
                """,
                (
                    row["checkpoint_id"],
                    row["execution_id"],
                    row["kind"],
                    row["status"],
                    row["recovery_status"],
                    int(row["step_index"]),
                    int(row["step_count"]),
                    1 if row.get("approval_pending") else 0,
                    row["summary"],
                    json.dumps(row.get("restart_payload") or {}, separators=(",", ":")),
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            self._conn.commit()

    def get(self, checkpoint_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            return _row(row)

    def find_by_execution(self, kind: str, execution_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM workflow_checkpoints
                WHERE kind = ? AND execution_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (kind, execution_id),
            ).fetchone()
            return _row(row)

    def list_recovery(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM workflow_checkpoints
                WHERE recovery_status IN ('interrupted', 'needs_review', 'active')
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [_row(row) for row in rows if row is not None]

    def list_active(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_checkpoints WHERE recovery_status = 'active'"
            ).fetchall()
            return [_row(row) for row in rows if row is not None]

    def mark_recovery(self, checkpoint_id: str, recovery_status: str, *, status: str | None = None, updated_at: str) -> bool:
        with self._lock:
            row = self.get(checkpoint_id)
            if row is None:
                return False
            if status is not None:
                self._conn.execute(
                    """
                    UPDATE workflow_checkpoints
                    SET recovery_status = ?, status = ?, approval_pending = 0, updated_at = ?
                    WHERE checkpoint_id = ?
                    """,
                    (recovery_status, status, updated_at, checkpoint_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE workflow_checkpoints
                    SET recovery_status = ?, approval_pending = 0, updated_at = ?
                    WHERE checkpoint_id = ?
                    """,
                    (recovery_status, updated_at, checkpoint_id),
                )
            self._conn.commit()
            return True


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    try:
        payload = json.loads(row["restart_payload"] or "{}")
    except json.JSONDecodeError:
        payload = None
    return {
        "checkpoint_id": row["checkpoint_id"],
        "execution_id": row["execution_id"],
        "kind": row["kind"],
        "status": row["status"],
        "recovery_status": row["recovery_status"],
        "step_index": int(row["step_index"]),
        "step_count": int(row["step_count"]),
        "approval_pending": bool(row["approval_pending"]),
        "summary": row["summary"],
        "restart_payload": payload,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
