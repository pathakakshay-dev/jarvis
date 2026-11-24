"""Explicit skill registry. Shell skills are rejected and nothing is registered in Phase 1."""

from __future__ import annotations

from jarvis.safety.policy import assert_skill_allowed
from jarvis.skills.base import SkillSpec


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, object] = {}

    def register(self, skill: object) -> None:
        spec = getattr(skill, "spec", None)
        if not isinstance(spec, SkillSpec):
            raise ValueError("skill is missing a spec")
        assert_skill_allowed(spec)
        self._skills[spec.id] = skill

    def get(self, skill_id: str) -> object | None:
        return self._skills.get(skill_id)

    def ids(self) -> list[str]:
        return list(self._skills)
