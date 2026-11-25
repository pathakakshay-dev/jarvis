"""Validate future desktop actions. This module never controls the desktop."""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass

from jarvis.audit import AuditLog
from jarvis.desktop.plan import ALLOWED_APPLICATIONS
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

EXECUTION_ENABLED = False
APPROVAL_POLICY = "Each desktop action requires its own explicit approval."
RISK_POLICY = "Unknown or ambiguous actions are High risk and are rejected. High-risk actions are never approved automatically."
_TTL_S = 120.0
_MAX_PENDING = 20
_MAX_KEPT = 100
_ACTION_TYPES = {
    "focus_application": "Low",
    "open_application": "Medium",
    "close_application": "High",
}
_FIELDS = frozenset({"action_type", "application", "description", "summary", "risk"})
_RISKS = frozenset({"Low", "Medium", "High"})
_ACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,']{2,159}$")
_UNSAFE = re.compile(
    r"(powershell|cmd\.exe|\bcmd\b|\bbash\b|subprocess|\.exe|https?:|javascript:|"
    r"\\\\|/|<|>|`|\$|%|\{|\}|\[|\]|\||&|;|:|"
    r"\b(shell|execute|click|type|launch|screenshot|clipboard|password|token|secret)\b|"
    r"write_file|read_file|list_files|search_files|create_folder|system_info|app_inventory|clipboard_read|"
    r"ignore previous|ignore all|disregard|you are now|system prompt|approve all|auto[- ]?approve)",
    re.I,
)
_DETAILS = {
    "malformed": "That desktop action is not valid.",
    "missing_field": "That desktop action is not valid.",
    "unexpected_field": "That desktop action is not valid.",
    "unknown_type": "That desktop action is not allowed.",
    "risk_mismatch": "That desktop action is not allowed.",
    "invalid_target": "That desktop target is not allowed.",
    "reused_id": "That desktop action is not valid.",
    "limit": "That desktop action is not valid.",
    "forged_id": "That desktop action is not waiting for approval.",
    "duplicate": "That desktop action is not waiting for approval.",
    "invalid_decision": "That desktop action is not waiting for approval.",
    "expired": "That desktop action has expired.",
    "cancelled": "The desktop action was cancelled.",
    "stopped": "The desktop action was cancelled.",
    "denied": "The desktop action was denied.",
    "automatic": "Each desktop action needs its own explicit approval.",
    "high_risk": "High-risk desktop actions cannot be approved automatically.",
    "disabled": "Desktop execution is disabled.",
}


@dataclass
class _Action:
    action_id: str
    action_type: str
    application: str
    description: str
    summary: str
    risk: str
    expires_at: float
    state: str
    generation: int
    order: int


class DesktopSafety:
    """Ledger for future desktop actions. Approval here does not run anything."""

    def __init__(self, halt: HaltController, audit: AuditLog, *, clock=None, ids: list[str] | None = None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid desktop action lifetime")
        self._halt = halt
        self._audit = audit
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._ids = list(ids) if ids is not None else None
        self._id_index = 0
        self._issued: set[str] = set()
        self._actions: dict[str, _Action] = {}
        self._mock_claimed: set[str] = set()
        self._stop_noted: set[int] = set()
        self._order = 0
        self._lock = threading.Lock()

    def report(self) -> dict:
        with self._lock:
            self._sync_locked()
            pending = sum(1 for action in self._actions.values() if action.state == "pending")
            return {
                "foundation": "active",
                "execution": "disabled",
                "execution_permitted": False,
                "executed": False,
                "replayed": False,
                "approval_policy": APPROVAL_POLICY,
                "risk_policy": RISK_POLICY,
                "emergency_stop": "stopped" if self._halt.halted else "idle",
                "pending_actions": pending,
            }

    def propose(self, payload: object) -> dict:
        with self._lock:
            self._sync_locked()
            if self._halt.halted:
                return self._fail("stopped", audit_status="desktop_cancelled")
            try:
                cleaned = validate_desktop_request(payload)
            except _RequestError as exc:
                return self._fail(exc.error_type)
            pending = sum(1 for action in self._actions.values() if action.state == "pending")
            if pending >= _MAX_PENDING:
                return self._fail("limit")
            action_id, id_error = self._next_id_locked()
            if action_id is None:
                return self._fail(id_error or "reused_id")
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                self._issued.add(action_id)
                return self._fail("stopped", audit_status="desktop_cancelled")
            self._order += 1
            action = _Action(
                action_id=action_id,
                action_type=cleaned["action_type"],
                application=cleaned["application"],
                description=cleaned["description"],
                summary=cleaned["summary"],
                risk=cleaned["risk"],
                expires_at=self._clock() + self._ttl_s,
                state="pending",
                generation=generation,
                order=self._order,
            )
            self._issued.add(action_id)
            self._actions[action_id] = action
            self._trim_locked()
            try:
                self._halt.request_approval(action_id)
            except HaltedError:
                action.state = "cancelled"
                return self._fail("stopped", audit_status="desktop_cancelled")
            if not self._halt.is_current(generation) or self._halt.approval_state(action_id) != "pending":
                action.state = "cancelled"
                return self._fail("stopped", audit_status="desktop_cancelled")
            self._audit.write(
                status="desktop_proposed",
                skill_id=action.action_type,
                outcome="pending",
                detail="Desktop action proposed",
            )
            return {
                "ok": True,
                "detail": "Desktop execution is disabled.",
                "action": _view(action),
                "execution_permitted": False,
                "executed": False,
                "replayed": False,
            }

    def decide(self, action_id: object, approved: object, *, automatic: bool = False) -> dict:
        with self._lock:
            self._sync_locked()
            if type(automatic) is not bool:
                return self._fail("invalid_decision")
            action = self._find_locked(action_id)
            if action is None:
                return self._fail("forged_id")
            if automatic:
                error_type = "high_risk" if action.risk == "High" else "automatic"
                return self._fail(error_type, skill_id=action.action_type, audit_status="desktop_denied", outcome="denied")
            if action.state == "approved":
                return self._fail("duplicate", skill_id=action.action_type)
            if action.state == "denied":
                return self._fail("duplicate", skill_id=action.action_type)
            if action.state == "expired":
                self._block_approval_locked(action)
                return self._fail("expired", skill_id=action.action_type)
            if action.state != "pending":
                return self._fail("cancelled", skill_id=action.action_type)
            if not self._halt.is_current(action.generation) or self._halt.approval_state(action.action_id) != "pending":
                action.state = "cancelled"
                self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")
                return self._fail("cancelled", skill_id="", audit=False)
            if self._clock() >= action.expires_at:
                action.state = "expired"
                self._block_approval_locked(action)
                self._audit.write(status="desktop_rejected", skill_id=action.action_type, outcome="rejected", error_type="expired", detail=_DETAILS["expired"])
                return self._closed("expired", "expired", action)
            if type(approved) is not bool:
                return self._fail("invalid_decision", skill_id=action.action_type)
            decision = "approved" if approved else "denied"
            if not self._halt.resolve_approval(action.action_id, decision):
                action.state = "cancelled"
                self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")
                return self._closed("cancelled", "cancelled")
            if not self._halt.is_current(action.generation):
                action.state = "cancelled"
                self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")
                return self._closed("cancelled", "cancelled")
            action.state = decision
            if decision == "denied":
                self._audit.write(status="desktop_denied", skill_id=action.action_type, outcome="denied", detail="Desktop approval denied")
                return self._closed("denied", "denied", action)
            self._audit.write(status="desktop_approved", skill_id=action.action_type, outcome="approved", detail="Desktop approval granted")
            body = self._closed("disabled", "approved", action)
            body["ok"] = True
            body["detail"] = "The approval was recorded. Desktop execution is disabled."
            return body

    def cancel(self, action_id: object) -> dict:
        with self._lock:
            self._sync_locked()
            action = self._find_locked(action_id)
            if action is None:
                return self._fail("forged_id")
            if action.state != "pending":
                return self._fail("cancelled", skill_id=action.action_type)
            action.state = "cancelled"
            self._block_approval_locked(action)
            self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")
            return self._closed("cancelled", "cancelled", action)

    def claim_for_mock(self, action_id: object) -> dict | None:
        """Return one current explicit approval for the mock simulator.

        Real desktop execution stays disabled. A claimed id cannot be claimed again.
        A stopped, stale, denied, or unapproved action is not returned.
        """
        with self._lock:
            self._sync_locked()
            action = self._find_locked(action_id)
            if action is None or action.state != "approved" or action.action_id in self._mock_claimed:
                return None
            if self._halt.halted or not self._halt.is_current(action.generation):
                return None
            self._mock_claimed.add(action.action_id)
            if self._halt.halted or not self._halt.is_current(action.generation):
                return None
            view = _view(action)
            view["execution_permitted"] = False
            return view

    def reject_execution(self, action_id: object = None) -> dict:
        """Refuse every desktop action. There is no execution implementation."""
        del action_id
        with self._lock:
            self._audit.write(status="desktop_rejected", outcome="rejected", error_type="disabled", detail="Desktop execution is disabled")
            return self._closed("disabled", "rejected")

    def observe_stop(self) -> None:
        with self._lock:
            self._sync_locked()

    def get(self, action_id: object) -> dict | None:
        with self._lock:
            self._sync_locked()
            action = self._find_locked(action_id)
            if action is None:
                return None
            self._mark_expired_locked(action)
            return _view(action)

    def _sync_locked(self) -> None:
        generation = self._halt.generation
        first_stop_note = generation not in self._stop_noted and (
            self._halt.halted or any(action.state == "pending" and not self._halt.is_current(action.generation) for action in self._actions.values())
        )
        cancelled: list[_Action] = []
        for action in self._actions.values():
            if action.state != "pending":
                continue
            if not self._halt.is_current(action.generation) or self._halt.approval_state(action.action_id) != "pending":
                action.state = "cancelled"
                cancelled.append(action)
        if first_stop_note:
            self._stop_noted.add(generation)
            for action in cancelled:
                self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")
            self._audit.write(status="desktop_stopped", outcome="cancelled", detail="Desktop emergency stop")
        else:
            for action in cancelled:
                self._audit.write(status="desktop_cancelled", skill_id=action.action_type, outcome="cancelled", detail="Desktop action cancelled")

    def _mark_expired_locked(self, action: _Action) -> None:
        if action.state == "pending" and self._clock() >= action.expires_at:
            action.state = "expired"
            self._block_approval_locked(action)

    def _find_locked(self, action_id: object) -> _Action | None:
        if not isinstance(action_id, str) or not _ACTION_ID.fullmatch(action_id):
            return None
        return self._actions.get(action_id)

    def _next_id_locked(self) -> tuple[str | None, str]:
        if self._ids is not None:
            if self._id_index >= len(self._ids):
                return None, "reused_id"
            candidate = self._ids[self._id_index]
            self._id_index += 1
            if not isinstance(candidate, str) or not _ACTION_ID.fullmatch(candidate):
                return None, "reused_id"
            if candidate in self._issued or self._halt.approval_state(candidate) is not None:
                return None, "reused_id"
            return candidate, ""
        for _ in range(3):
            candidate = uuid.uuid4().hex
            if candidate not in self._issued and self._halt.approval_state(candidate) is None:
                return candidate, ""
        return None, "reused_id"

    def _block_approval_locked(self, action: _Action) -> None:
        """Stop a cancelled or expired id from being approved later. This does not run the action."""
        if self._halt.halted:
            return
        if self._halt.approval_state(action.action_id) == "pending":
            self._halt.resolve_approval(action.action_id, "denied")

    def _trim_locked(self) -> None:
        finished = [action for action in self._actions.values() if action.state != "pending"]
        finished.sort(key=lambda action: action.order)
        while len(self._actions) > _MAX_KEPT and finished:
            old = finished.pop(0)
            self._actions.pop(old.action_id, None)

    def _fail(
        self,
        error_type: str,
        *,
        skill_id: str = "",
        audit_status: str = "desktop_rejected",
        outcome: str = "",
        audit: bool = True,
    ) -> dict:
        if audit:
            if not outcome:
                outcome = "rejected"
                if audit_status == "desktop_denied":
                    outcome = "denied"
                elif audit_status == "desktop_cancelled":
                    outcome = "cancelled"
            self._audit.write(
                status=audit_status,
                skill_id=skill_id,
                outcome=outcome,
                error_type=error_type,
                detail=_DETAILS.get(error_type, "That desktop action is not valid."),
            )
        return self._closed(error_type, "rejected")

    def _closed(self, error_type: str, state: str, action: _Action | None = None) -> dict:
        body = {
            "ok": False,
            "detail": _DETAILS.get(error_type, "That desktop action is not valid."),
            "risk": "High",
            "state": state,
            "execution_permitted": False,
            "executed": False,
            "replayed": False,
        }
        if action is not None:
            body["action"] = _view(action)
            body["risk"] = action.risk
            body["state"] = action.state
        return body


class _RequestError(Exception):
    def __init__(self, error_type: str) -> None:
        self.error_type = error_type


def validate_desktop_request(payload: object) -> dict:
    """Check one untrusted action request. The returned record is still not executable."""
    if not isinstance(payload, dict):
        raise _RequestError("malformed")
    if set(payload) != _FIELDS:
        missing = _FIELDS - set(payload)
        raise _RequestError("missing_field" if missing and set(payload) < _FIELDS else "unexpected_field")
    action_type = payload["action_type"]
    application = payload["application"]
    description = payload["description"]
    summary = payload["summary"]
    risk = payload["risk"]
    if not all(isinstance(value, str) for value in (action_type, application, description, summary, risk)):
        raise _RequestError("malformed")
    if action_type not in _ACTION_TYPES:
        raise _RequestError("unknown_type")
    expected_risk = _ACTION_TYPES[action_type]
    if risk not in _RISKS or risk != expected_risk:
        raise _RequestError("risk_mismatch")
    if application not in ALLOWED_APPLICATIONS:
        raise _RequestError("invalid_target")
    description = _plain(description)
    summary = _plain(summary)
    if description is None or summary is None:
        raise _RequestError("invalid_target")
    return {
        "action_type": action_type,
        "application": application,
        "description": description,
        "summary": summary,
        "risk": expected_risk,
    }


def _plain(value: str) -> str | None:
    if any(ord(character) < 32 for character in value):
        return None
    cleaned = " ".join(value.split())
    if cleaned != value.strip():
        return None
    if not _PLAIN.fullmatch(cleaned) or _UNSAFE.search(cleaned) or looks_like_secret(cleaned):
        return None
    return cleaned


def _view(action: _Action) -> dict:
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "application": action.application,
        "description": action.description,
        "summary": action.summary,
        "risk": action.risk,
        "required_approval": True,
        "expires_at": action.expires_at,
        "state": action.state,
        "execution_permitted": False,
        "review_required": action.risk == "High",
    }


def _monotonic() -> float:
    import time

    return time.monotonic()
