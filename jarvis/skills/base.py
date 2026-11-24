"""Skill contract. Phase 1 registers no skills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillSpec:
    id: str
    description: str
    permission: Any
    parameters: dict = field(default_factory=dict)
    timeout_s: float = 20


@dataclass
class SkillResult:
    ok: bool
    text: str = ""
    outcome: str = ""
    error_type: str = ""
