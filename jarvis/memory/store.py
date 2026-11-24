"""SQLite conversation store. Startup never treats old rows as jobs to run."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()
        self.conversation_id = self._latest_conversation()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                message_id UNINDEXED
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                fact,
                memory_id UNINDEXED
            );
            """
        )
        self._conn.commit()

    def _latest_conversation(self) -> int | None:
        row = self._conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else int(row["id"])

    def add_message(self, role: str, content: str, status: str) -> int:
        with self._lock:
            if self.conversation_id is None:
                cursor = self._conn.execute(
                    "INSERT INTO conversations (created_at) VALUES (?)",
                    (_now(),),
                )
                self.conversation_id = int(cursor.lastrowid)
            cursor = self._conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.conversation_id, role, content, status, _now()),
            )
            message_id = int(cursor.lastrowid)
            self._conn.execute(
                "INSERT INTO messages_fts (content, message_id) VALUES (?, ?)",
                (content, message_id),
            )
            self._conn.commit()
            return message_id

    def recent(self, limit: int = 20) -> list[dict]:
        if self.conversation_id is None:
            return []
        with self._lock:
            return self._recent_unlocked(limit)

    def _recent_unlocked(self, limit: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = ? AND status = 'ok' AND role IN ('user', 'assistant')
            ORDER BY id DESC LIMIT ?
            """,
            (self.conversation_id, limit),
        ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def visible(self) -> list[dict]:
        if self.conversation_id is None:
            return []
        with self._lock:
            return self._visible_unlocked()

    def _visible_unlocked(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT role, content, status, created_at FROM messages
            WHERE conversation_id = ? AND status IN ('ok', 'failed')
            ORDER BY id ASC
            """,
            (self.conversation_id,),
        ).fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search(self, query: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH ? LIMIT 20",
            (query,),
        ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def add_memory(self, fact: str) -> tuple[dict, bool]:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, fact, created_at, updated_at FROM memories WHERE lower(fact) = lower(?)",
                (fact,),
            ).fetchone()
            if existing is not None:
                return _memory_row(existing), False
            now = _now()
            cursor = self._conn.execute(
                "INSERT INTO memories (fact, created_at, updated_at) VALUES (?, ?, ?)",
                (fact, now, now),
            )
            memory_id = int(cursor.lastrowid)
            self._conn.execute(
                "INSERT INTO memories_fts (fact, memory_id) VALUES (?, ?)",
                (fact, memory_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, fact, created_at, updated_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return _memory_row(row), True

    def list_memories(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fact, created_at, updated_at FROM memories ORDER BY id ASC"
            ).fetchall()
            return [_memory_row(row) for row in rows]

    def get_memory(self, memory_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, fact, created_at, updated_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return None if row is None else _memory_row(row)

    def update_memory(self, memory_id: int, fact: str) -> dict | None:
        with self._lock:
            current = self._conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if current is None:
                return None
            now = _now()
            self._conn.execute(
                "UPDATE memories SET fact = ?, updated_at = ? WHERE id = ?",
                (fact, now, memory_id),
            )
            self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self._conn.execute(
                "INSERT INTO memories_fts (fact, memory_id) VALUES (?, ?)",
                (fact, memory_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, fact, created_at, updated_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return _memory_row(row)

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock:
            current = self._conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if current is None:
                return False
            self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
            return True

    def clear_memories(self) -> int:
        with self._lock:
            count = int(self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
            self._conn.execute("DELETE FROM memories_fts")
            self._conn.execute("DELETE FROM memories")
            self._conn.commit()
            return count

    def search_memories(self, query: str, limit: int = 20) -> list[dict]:
        from jarvis.memory.commands import fts_query

        match = fts_query(query)
        if not match:
            return []
        return self._match_memories(match, limit)

    def relevant_memories(self, text: str, limit: int = 5) -> list[dict]:
        from jarvis.memory.commands import fts_query

        match = fts_query(text)
        if not match:
            return []
        return self._match_memories(match, limit)

    def _match_memories(self, match: str, limit: int) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT memories.id, memories.fact, memories.created_at, memories.updated_at
                    FROM memories_fts
                    JOIN memories ON memories.id = memories_fts.memory_id
                    WHERE memories_fts MATCH ?
                    ORDER BY memories.id ASC
                    LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [_memory_row(row) for row in rows]

    def close(self) -> None:
        self._conn.close()


def _memory_row(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "fact": row["fact"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
