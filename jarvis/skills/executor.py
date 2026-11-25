"""Run one registered skill. Model text is never treated as code or a shell command."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.policy import SHELL_IDS, Policy
from jarvis.skills.base import SkillResult, SkillSpec
from jarvis.skills.registry import SkillRegistry
from jarvis.skills.schema import validate_arguments


@dataclass
class _Pending:
    skill_id: str
    arguments: dict
    generation: int


class SkillExecutor:
    def __init__(self, registry: SkillRegistry, policy: Policy, halt: HaltController, audit: AuditLog) -> None:
        self._registry = registry
        self._policy = policy
        self._halt = halt
        self._audit = audit
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def catalog(self) -> list[dict]:
        rows = []
        for skill_id in self._registry.ids():
            skill = self._registry.get(skill_id)
            spec = getattr(skill, "spec", None)
            if not isinstance(spec, SkillSpec):
                continue
            permission = getattr(spec.permission, "value", spec.permission)
            rows.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": permission,
                    "input_schema": dict(spec.parameters),
                    "enabled": self._policy.decide(spec) != "disabled",
                }
            )
        return rows

    def pending(self) -> list[dict]:
        with self._lock:
            return self._pending_rows()

    def _pending_rows(self) -> list[dict]:
        rows = []
        for action_id, item in list(self._pending.items()):
            if not self._halt.is_current(item.generation) or self._halt.approval_state(action_id) != "pending":
                self._pending.pop(action_id, None)
                continue
            path = item.arguments.get("path", "")
            query = item.arguments.get("query", "")
            mode = item.arguments.get("mode", "")
            row = {
                "action_id": action_id,
                "skill": item.skill_id,
                "path": path if isinstance(path, str) else "",
                "query": query if isinstance(query, str) else "",
                "mode": mode if isinstance(mode, str) else "",
            }
            if item.skill_id == "write_file":
                content = item.arguments.get("content", "")
                row["content"] = content if isinstance(content, str) else ""
            rows.append(row)
        return rows

    def pending_skill(self, action_id: str) -> str:
        with self._lock:
            item = self._pending.get(action_id)
            return item.skill_id if item is not None else ""

    def requested_path(self, action_id: str, skill_id: str = "read_file") -> str:
        with self._lock:
            item = self._pending.get(action_id)
            if item is None or item.skill_id != skill_id:
                return ""
            key = "query" if skill_id == "search_files" else "path"
            value = item.arguments.get(key, "")
            return value if isinstance(value, str) else ""

    def submit(self, skill_id: str, arguments: dict | None = None) -> SkillResult:
        with self._lock:
            return self._submit(skill_id, arguments)

    def _submit(self, skill_id: str, arguments: dict | None) -> SkillResult:
        generation = self._halt.capture()
        if not self._halt.is_current(generation):
            self._audit.write(status="stopped", skill_id=str(skill_id), outcome="cancelled", error_type="stopped", detail="Skill request refused")
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        prepared = self._prepare(skill_id, arguments if arguments is not None else {})
        if isinstance(prepared, SkillResult):
            return prepared
        skill, spec, cleaned = prepared
        self._audit.write(status="requested", skill_id=spec.id, detail="Skill request")
        decision = self._policy.decide(spec)
        if decision == "disabled":
            self._audit.write(status="denied", skill_id=spec.id, outcome="denied", error_type="disabled", detail="Skill is not enabled")
            return SkillResult(False, text="That skill is not enabled.", outcome="disabled", error_type="disabled")
        if not callable(getattr(skill, "execute", None)):
            self._audit.write(status="error", skill_id=spec.id, outcome="rejected", error_type="not_executable", detail="Skill has no executor")
            return SkillResult(False, text="This skill cannot be run from the executor.", outcome="rejected", error_type="not_executable")
        if decision == "needs_approval" or spec.requires_approval:
            action_id = uuid.uuid4().hex
            self._pending[action_id] = _Pending(spec.id, cleaned, generation)
            try:
                self._halt.request_approval(action_id)
            except HaltedError:
                self._pending.pop(action_id, None)
                self._audit.write(status="stopped", skill_id=spec.id, outcome="cancelled", error_type="stopped", detail="Skill request refused")
                return SkillResult(False, outcome="cancelled", error_type="stopped")
            self._audit.write(status="requesting_approval", skill_id=spec.id, detail="One action is waiting for approval")
            return SkillResult(False, text="That action needs approval.", outcome="needs_approval", error_type="needs_approval", action_id=action_id)
        return self._invoke(skill, spec, cleaned, generation)

    def confirm(self, action_id: str, *, approved: bool) -> SkillResult:
        with self._lock:
            return self._confirm(action_id, approved=approved)

    def _confirm(self, action_id: str, *, approved: bool) -> SkillResult:
        pending = self._pending.get(action_id)
        if pending is None or not isinstance(action_id, str):
            return SkillResult(False, text="That action is not waiting for approval.", outcome="rejected", error_type="unknown_action")
        if not self._halt.is_current(pending.generation) or self._halt.approval_state(action_id) != "pending":
            self._pending.pop(action_id, None)
            self._audit.write(status="stopped", skill_id=pending.skill_id, outcome="cancelled", error_type="stopped", detail="Cancelled skill action was not run")
            return SkillResult(False, outcome="cancelled", error_type="stopped", action_id=action_id)
        skill = self._registry.get(pending.skill_id)
        spec = getattr(skill, "spec", None)
        if skill is None or not isinstance(spec, SkillSpec):
            self._pending.pop(action_id, None)
            return SkillResult(False, text="Unknown skill.", outcome="rejected", error_type="unknown_skill", action_id=action_id)
        if not approved:
            self._halt.resolve_approval(action_id, "denied")
            self._pending.pop(action_id, None)
            self._audit.write(status="denied", skill_id=spec.id, outcome="denied", error_type="denied", detail="Skill action denied")
            return SkillResult(False, text="The action was denied.", outcome="denied", error_type="denied", action_id=action_id)
        if self._policy.decide(spec) == "disabled":
            self._audit.write(status="denied", skill_id=spec.id, outcome="denied", error_type="disabled", detail="Skill is not enabled")
            return SkillResult(False, text="That skill is not enabled.", outcome="disabled", error_type="disabled", action_id=action_id)
        if not self._halt.resolve_approval(action_id, "approved"):
            self._pending.pop(action_id, None)
            self._audit.write(status="stopped", skill_id=spec.id, outcome="cancelled", error_type="stopped", detail="Skill action was not approved")
            return SkillResult(False, outcome="cancelled", error_type="stopped", action_id=action_id)
        self._pending.pop(action_id, None)
        self._audit.write(status="approved", skill_id=spec.id, outcome="approved", detail="One skill action approved")
        if not self._halt.is_current(pending.generation):
            self._audit.write(status="stopped", skill_id=spec.id, outcome="cancelled", error_type="stopped", detail="Approved skill action was not run")
            return SkillResult(False, outcome="cancelled", error_type="stopped", action_id=action_id)
        if self._policy.decide(spec) == "disabled":
            self._audit.write(status="denied", skill_id=spec.id, outcome="denied", error_type="disabled", detail="Skill is not enabled")
            return SkillResult(False, text="That skill is not enabled.", outcome="disabled", error_type="disabled", action_id=action_id)
        return self._invoke(skill, spec, pending.arguments, pending.generation, action_id)

    def _prepare(self, skill_id: object, arguments: object) -> tuple[object, SkillSpec, dict] | SkillResult:
        if not isinstance(skill_id, str) or skill_id.strip().lower() in SHELL_IDS:
            self._audit.write(status="denied", skill_id=str(skill_id)[:80], outcome="denied", error_type="shell_disabled", detail="Shell execution is disabled")
            return SkillResult(False, text="Shell execution is disabled.", outcome="rejected", error_type="shell_disabled")
        skill = self._registry.get(skill_id)
        spec = getattr(skill, "spec", None)
        if skill is None or not isinstance(spec, SkillSpec) or spec.id != skill_id:
            self._audit.write(status="denied", skill_id=skill_id, outcome="denied", error_type="unknown_skill", detail="Unknown skill")
            return SkillResult(False, text="Unknown skill.", outcome="rejected", error_type="unknown_skill")
        try:
            cleaned = validate_arguments(spec.parameters, arguments)
        except ValueError:
            self._audit.write(status="denied", skill_id=spec.id, outcome="denied", error_type="invalid_arguments", detail="Skill arguments were rejected")
            return SkillResult(False, text="Those arguments are not valid for that skill.", outcome="rejected", error_type="invalid_arguments")
        return skill, spec, cleaned

    def _invoke(self, skill: object, spec: SkillSpec, arguments: dict, generation: int, action_id: str = "") -> SkillResult:
        if not self._halt.is_current(generation):
            self._audit.write(status="stopped", skill_id=spec.id, outcome="cancelled", error_type="stopped", detail="Skill execution blocked")
            return SkillResult(False, outcome="cancelled", error_type="stopped", action_id=action_id)
        try:
            result = skill.execute(arguments)  # type: ignore[attr-defined]
        except Exception:
            self._audit.write(status="error", skill_id=spec.id, outcome="error", error_type="execution_error", detail="Skill execution failed")
            return SkillResult(False, text="The skill failed.", outcome="error", error_type="execution_error", action_id=action_id)
        if not isinstance(result, SkillResult):
            self._audit.write(status="error", skill_id=spec.id, outcome="error", error_type="execution_error", detail="Skill returned an invalid result")
            return SkillResult(False, text="The skill failed.", outcome="error", error_type="execution_error", action_id=action_id)
        if not self._halt.is_current(generation):
            self._audit.write(status="stopped", skill_id=spec.id, outcome="cancelled", error_type="stopped", detail="Skill result discarded after stop")
            return SkillResult(False, outcome="cancelled", error_type="stopped", action_id=action_id)
        self._audit.write(status="executed", skill_id=spec.id, outcome="completed" if result.ok else "error", error_type=result.error_type, detail="Skill finished")
        result.action_id = action_id
        return result
