"""Local reminder delivery. No shell, email, or external integrations."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    reminder_id: str
    title: str
    due_at: str
    kind: str  # delivered | missed
    at: str


class Notifier:
    def notify(self, notice: Notice) -> bool:
        raise NotImplementedError


class QueueNotifier(Notifier):
    """In-process notification queue for the UI and tests."""

    def __init__(self, *, limit: int = 50) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._items: list[Notice] = []
        self.calls: list[Notice] = []

    def notify(self, notice: Notice) -> bool:
        with self._lock:
            self.calls.append(notice)
            self._items.append(notice)
            if len(self._items) > self._limit:
                self._items = self._items[-self._limit :]
        return True

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._items[-limit:]
        return [
            {
                "reminder_id": item.reminder_id,
                "title": item.title,
                "due_at": item.due_at,
                "kind": item.kind,
                "at": item.at,
            }
            for item in rows
        ]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
