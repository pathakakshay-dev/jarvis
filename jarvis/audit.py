"""Append-only audit log. Entries never store API keys."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_BEARER = re.compile(r"(?i)bearer\s+\S+")
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|x-goog-api-key)\s*[:=]?\s*\S+")


def scrub(text: str) -> str:
    text = _BEARER.sub("bearer [redacted]", text)
    return _SECRET.sub(lambda match: match.group(1) + "=[redacted]", text)


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        status: str,
        skill_id: str = "",
        outcome: str = "",
        error_type: str = "",
        detail: str = "",
    ) -> None:
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "skill_id": skill_id,
            "outcome": outcome,
            "error_type": error_type,
            "detail": scrub(detail)[:500],
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def tail(self, limit: int = 30) -> list[dict]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        rows: list[dict] = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
