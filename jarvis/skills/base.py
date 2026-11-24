"""Skill contract. A registered skill stays off until policy allows it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillSpec:
    """name is the id. parameters is the input schema, not executable code."""

    id: str
    description: str
    permission: Any
    parameters: dict = field(default_factory=dict)
    timeout_s: float = 20
    requires_approval: bool = False

    @property
    def name(self) -> str:
        return self.id


@dataclass
class SkillResult:
    ok: bool
    text: str = ""
    outcome: str = ""
    error_type: str = ""
    action_id: str = ""
