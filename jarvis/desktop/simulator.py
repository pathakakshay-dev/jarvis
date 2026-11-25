"""Mock desktop lifecycle. This module never touches the real desktop."""

from __future__ import annotations

import threading

from jarvis.desktop.safety import DesktopSafety, validate_desktop_request

WARNING = "Simulated results do not represent real Windows actions."
_MODE = "mock"


class DesktopSimulator:
    """Propose, approve, and apply actions to an in-memory desktop only."""

    def __init__(self, safety: DesktopSafety) -> None:
        if not isinstance(safety, DesktopSafety):
            raise TypeError("simulator requires the desktop safety ledger")
        self._safety = safety
        self._open: set[str] = set()
        self._focused = ""
        self._history: list[dict] = []
        self._proposal: dict | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            self._refresh_locked()
            return self._state_locked()

    def propose(self, payload: object) -> dict:
        with self._lock:
            self._refresh_locked()
            before = self._fingerprint_locked()
            try:
                result = self._safety.propose(payload)
            except Exception:
                return self._closed_locked("The desktop simulator could not accept that action.", before)
            if not result.get("ok"):
                body = self._state_locked()
                body.update(
                    {
                        "ok": False,
                        "detail": str(result.get("detail") or "That desktop action is not valid."),
                        "simulated": False,
                        "replayed": False,
                        "execution_permitted": False,
                        "real_desktop": False,
                    }
                )
                return body
            action = result.get("action")
            if not isinstance(action, dict):
                return self._closed_locked("The desktop simulator could not accept that action.", before)
            self._cancel_previous_locked()
            proposal = {
                "action_id": action.get("action_id", ""),
                "action_type": action.get("action_type", ""),
                "application": action.get("application", ""),
                "risk": action.get("risk", ""),
                "summary": action.get("summary", ""),
                "preview": _preview(action),
                "state": "pending",
            }
            if not proposal["action_id"] or proposal["preview"] is None:
                try:
                    self._safety.cancel(action.get("action_id"))
                except Exception:
                    pass
                return self._closed_locked("The desktop simulator could not accept that action.", before)
            self._proposal = proposal
            self._refresh_locked()
            if self._proposal is None:
                return self._closed_locked("The desktop action was cancelled.", before)
            body = self._state_locked()
            body.update(
                {
                    "ok": True,
                    "detail": "This is a mock preview. Desktop execution is disabled.",
                    "simulated": False,
                    "replayed": False,
                    "execution_permitted": False,
                    "real_desktop": False,
                }
            )
            return body

    def decide(self, action_id: object, approved: object, *, automatic: bool = False) -> dict:
        with self._lock:
            self._refresh_locked()
            before = self._fingerprint_locked()
            if type(automatic) is not bool or automatic:
                detail = "Each desktop action needs its own explicit approval."
                try:
                    result = self._safety.decide(action_id, approved, automatic=True)
                    if isinstance(result, dict) and isinstance(result.get("detail"), str):
                        detail = result["detail"]
                except Exception:
                    detail = "Each desktop action needs its own explicit approval."
                return self._closed_locked(detail, before)
            try:
                result = self._safety.decide(action_id, approved)
            except Exception:
                return self._closed_locked("The desktop simulator could not record that decision.", before)
            if not result.get("ok"):
                self._restore_locked(before)
                self._refresh_locked()
                body = self._state_locked()
                body.update(
                    {
                        "ok": False,
                        "detail": str(result.get("detail") or "That desktop action is not waiting for approval."),
                        "simulated": False,
                        "replayed": False,
                        "execution_permitted": False,
                        "real_desktop": False,
                    }
                )
                return body
            claimed = None
            try:
                claimed = self._safety.claim_for_mock(action_id)
            except Exception:
                claimed = None
            if not isinstance(claimed, dict):
                self._restore_locked(before)
                body = self._state_locked()
                body.update(
                    {
                        "ok": False,
                        "detail": "The simulation was not applied.",
                        "simulated": False,
                        "replayed": False,
                        "execution_permitted": False,
                        "real_desktop": False,
                    }
                )
                return body
            changed, outcome = self._apply_locked(claimed)
            if self._safety_stopped():
                self._restore_locked(before)
                body = self._state_locked()
                body.update(
                    {
                        "ok": False,
                        "detail": "The desktop action was cancelled.",
                        "simulated": False,
                        "replayed": False,
                        "execution_permitted": False,
                        "real_desktop": False,
                    }
                )
                return body
            self._remember_locked(claimed, outcome, changed)
            body = self._state_locked()
            body.update(
                {
                    "ok": changed,
                    "detail": outcome,
                    "result": outcome,
                    "simulated": changed,
                    "replayed": False,
                    "execution_permitted": False,
                    "real_desktop": False,
                }
            )
            return body

    def reset(self) -> dict:
        """Clear the mock desktop only. Permissions, files, and stop state stay as they are."""
        with self._lock:
            self._open.clear()
            self._focused = ""
            self._history.clear()
            self._proposal = None
            body = self._state_locked()
            body.update(
                {
                    "ok": True,
                    "detail": "The mock desktop was reset. " + WARNING,
                    "simulated": False,
                    "replayed": False,
                    "execution_permitted": False,
                    "real_desktop": False,
                }
            )
            return body

    def observe_stop(self) -> None:
        with self._lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        try:
            self._safety.observe_stop()
        except Exception:
            return
        if self._proposal is None:
            return
        try:
            current = self._safety.get(self._proposal.get("action_id"))
        except Exception:
            self._proposal = None
            return
        if not isinstance(current, dict) or current.get("state") != "pending":
            self._proposal = None

    def _cancel_previous_locked(self) -> None:
        if self._proposal is None:
            return
        action_id = self._proposal.get("action_id")
        self._proposal = None
        try:
            self._safety.cancel(action_id)
        except Exception:
            return

    def _apply_locked(self, action: dict) -> tuple[bool, str]:
        if not isinstance(action, dict):
            return False, "The simulator left the mock desktop unchanged."
        try:
            cleaned = validate_desktop_request(
                {
                    "action_type": action.get("action_type"),
                    "application": action.get("application"),
                    "description": action.get("description"),
                    "summary": action.get("summary"),
                    "risk": action.get("risk"),
                }
            )
        except Exception:
            return False, "The simulator left the mock desktop unchanged."
        kind = cleaned["action_type"]
        name = cleaned["application"]
        if kind == "open_application":
            self._open.add(name)
            return True, name + " is open in the simulator."
        if kind == "focus_application":
            if name not in self._open:
                return False, "The mock application was not open, so the simulator made no change."
            self._focused = name
            return True, name + " is focused in the simulator."
        if kind == "close_application":
            if name not in self._open:
                return False, "The mock application was not open, so the simulator made no change."
            self._open.discard(name)
            if self._focused == name:
                self._focused = ""
            return True, name + " is closed in the simulator."
        return False, "The simulator left the mock desktop unchanged."

    def _remember_locked(self, action: dict, outcome: str, changed: bool) -> None:
        self._history.append(
            {
                "action_id": action.get("action_id", ""),
                "action_type": action.get("action_type", ""),
                "application": action.get("application", ""),
                "result": outcome,
                "changed": changed,
            }
        )
        del self._history[:-30]
        if self._proposal and self._proposal.get("action_id") == action.get("action_id"):
            self._proposal = None

    def _safety_stopped(self) -> bool:
        try:
            return self._safety.report().get("emergency_stop") == "stopped"
        except Exception:
            return True

    def _fingerprint_locked(self) -> tuple:
        return (set(self._open), self._focused, [dict(item) for item in self._history])

    def _restore_locked(self, fingerprint: tuple) -> None:
        opened, focused, history = fingerprint
        self._open = set(opened)
        self._focused = focused
        self._history = [dict(item) for item in history]
        self._refresh_locked()

    def _closed_locked(self, detail: str, fingerprint: tuple) -> dict:
        self._restore_locked(fingerprint)
        body = self._state_locked()
        body.update(
            {
                "ok": False,
                "detail": detail,
                "simulated": False,
                "replayed": False,
                "execution_permitted": False,
                "real_desktop": False,
            }
        )
        return body

    def _state_locked(self) -> dict:
        latest = self._history[-1]["result"] if self._history else ""
        return {
            "mode": _MODE,
            "warning": WARNING,
            "execution": "disabled",
            "execution_permitted": False,
            "real_desktop": False,
            "open_applications": sorted(self._open),
            "focused": self._focused,
            "result": latest,
            "history": [dict(item) for item in self._history],
            "proposal": dict(self._proposal) if self._proposal else None,
        }


def _preview(action: dict) -> str | None:
    summary = action.get("summary")
    application = action.get("application")
    risk = action.get("risk")
    if not isinstance(summary, str) or not isinstance(application, str) or not isinstance(risk, str):
        return None
    return "Mock preview. " + summary + " Target application: " + application + ". Risk: " + risk + ". " + WARNING
