"""Browser Workflow Agent over Browser Bridge and Browser Actions.

Plans are validated locally (no provider). Execution arms one step at a time,
reuses Browser Actions for click/focus/type/select-all, and advances only after
a verified action result. Isolated from Goal Agent, desktop engine, and planner.
"""

from __future__ import annotations

import re
import threading
import uuid

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_MAX_STEPS = 5
_ID = re.compile(r"^[0-9a-f]{32}$")
_ELEMENT_ID = re.compile(r"^[0-9a-f]{16}$")
_TEXT = re.compile(r"^[A-Za-z0-9 .,!?'\-]{1,200}$")
_ACTIONS = frozenset({"observe", "click", "focus", "type", "select_all"})
_RUN_DONE = frozenset({"completed", "failed", "stopped", "cancelled", "denied"})


class BrowserWorkflow:
    """Propose a short browser workflow, start it, and approve each step once."""

    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        bridge,
        lock: threading.RLock,
        clock=None,
        ttl_s: float = _TTL_S,
        recovery=None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid browser workflow lifetime")
        self._halt = halt
        self._audit = audit
        self._bridge = bridge
        self._lock = lock
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._recovery = recovery
        self._enabled_flag = False
        self._run: dict | None = None
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._notice = ""
        self._awaiting = ""  # "", "observe", "action_approval", "action_run"

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            return self._view_locked(False)

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled_flag = bool(enabled)
            if not self._enabled_flag:
                self._cancel_pending_locked()
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._finish_locked("cancelled", "Browser workflow is off.")
                else:
                    self._notice = "Browser workflow is off."
            else:
                self._notice = "Browser workflow is on. Propose a short plan after scanning targets."
            return self._enabled_flag

    def busy(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._pending is not None or (self._run is not None and self._run["status"] not in _RUN_DONE)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._cancel_pending_locked()
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._run["status"] = "stopped"
                    for step in self._run["steps"][self._run["index"] :]:
                        if step["state"] in {"pending", "waiting", "ready"}:
                            step["state"] = "stopped"
                    self._checkpoint_locked()
                self._awaiting = ""
                self._notice = ""

    def drop_state(self) -> None:
        with self._lock:
            self._cancel_pending_locked()
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._finish_locked("cancelled", "The browser bridge was disconnected.")
            self._awaiting = ""

    def clear(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._cancel_pending_locked()
            self._run = None
            self._awaiting = ""
            self._notice = "Browser workflow state was cleared."
            self._audit.write(status="browser_workflow_cleared", outcome="completed", detail="Browser workflow cleared")
            return self._view_locked(False)

    def cancel(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._cancel_pending_locked()
            actions = self._bridge.actions
            if actions.busy():
                actions.cancel()
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._finish_locked("cancelled", "The workflow was cancelled.")
            else:
                self._notice = "The workflow was cancelled."
            self._awaiting = ""
            self._audit.write(status="browser_workflow_cancelled", outcome="cancelled", detail="Browser workflow cancelled")
            return self._view_locked(False)

    def propose(self, payload: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The workflow was cancelled.")
            if not self._enabled_flag:
                self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow is not enabled")
                return self._stop_view("Browser workflow is not enabled.")
            if self._bridge.has_bridge_pending() or self._bridge.actions.busy():
                return self._stop_view("The browser bridge is busy.")
            if self._pending is not None or (self._run is not None and self._run["status"] not in _RUN_DONE):
                return self._stop_view("A browser workflow is already running.")
            session = self._bridge.session_snapshot()
            if session is None:
                return self._stop_view("Connect the browser bridge first.")
            try:
                steps = _steps(payload)
            except _WorkflowError as exc:
                self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow rejected")
                return self._stop_view(exc.text)
            prepared = []
            for step in steps:
                try:
                    prepared.append(self._prepare_step_locked(step))
                except _WorkflowError as exc:
                    self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow rejected")
                    return self._stop_view(exc.text)
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._stop_view("The workflow was cancelled.")
            plan_id = uuid.uuid4().hex
            self._run = {
                "plan_id": plan_id,
                "generation": generation,
                "status": "proposed",
                "index": 0,
                "steps": prepared,
                "expires": self._clock() + self._ttl_s,
                "session_id": session["session_id"],
            }
            self._awaiting = ""
            self._notice = "Review the browser workflow."
            self._audit.write(status="browser_workflow_proposed", outcome="pending", detail="Browser workflow proposed")
            self._checkpoint_locked()
            return self._view_locked(False)

    def start(self, plan_id: object, approved: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The workflow was cancelled.")
            if not self._enabled_flag:
                return self._stop_view("Browser workflow is not enabled.")
            run = self._run
            if run is None or not isinstance(plan_id, str) or plan_id != run["plan_id"]:
                return self._stop_view("That browser workflow is not waiting.")
            if run["status"] != "proposed":
                return self._view_locked(False)
            if type(approved) is not bool or not approved:
                self._finish_locked("denied", "The workflow was denied.")
                self._audit.write(status="browser_workflow_denied", outcome="denied", detail="Browser workflow denied")
                return self._view_locked(False)
            if not self._halt.is_current(run["generation"]):
                self._finish_locked("stopped", "The workflow was cancelled.")
                return self._view_locked(False)
            try:
                self._arm_step_locked()
            except _WorkflowError as exc:
                self._finish_locked("failed", exc.text)
                self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow failed")
                return self._view_locked(False)
            self._audit.write(status="browser_workflow_started", outcome="pending", detail="Browser workflow started")
            self._checkpoint_locked()
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id):
                self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow step rejected")
                return self._stop_view("That action is not waiting for approval.")
            if action_id in self._used:
                ready = self._bridge.actions._ready
                if self._awaiting == "action_run" and ready is not None and ready.get("action_id") == action_id:
                    return self._view_locked(True)
                self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow step rejected")
                return self._stop_view("That action is not waiting for approval.")
            if self._awaiting in {"action_approval", "action_run"}:
                return self._confirm_action_locked(action_id, approved)
            return self._confirm_observe_locked(action_id, approved)

    def on_action_result_locked(self, result: dict | None) -> None:
        """Called by BrowserActions while the shared lock is held."""
        run = self._run
        if run is None or run["status"] in _RUN_DONE or self._awaiting != "action_run":
            return
        if not isinstance(result, dict):
            self._finish_locked("failed", "The browser action failed verification.")
            return
        expected = run["steps"][run["index"]].get("action_id", "")
        if expected and result.get("action_id") and result.get("action_id") != expected:
            self._finish_locked("failed", "The browser action result did not match.")
            self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow result mismatch")
            return
        if result.get("outcome") == "completed" and result.get("verified") is True:
            self._mark_step_locked("completed")
            self._audit.write(status="browser_workflow_step", outcome="completed", detail="Browser workflow step completed")
            if run["index"] + 1 >= len(run["steps"]):
                self._finish_locked("completed", "The browser workflow finished.")
                self._audit.write(status="browser_workflow_completed", outcome="completed", detail="Browser workflow finished")
                return
            run["index"] += 1
            run["status"] = "waiting"
            self._awaiting = ""
            try:
                self._arm_step_locked()
            except _WorkflowError as exc:
                self._finish_locked("failed", exc.text)
                self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow failed")
            return
        self._mark_step_locked("failed")
        self._finish_locked("failed", "The browser action failed verification.")
        self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow step failed")

    def on_action_timeout_locked(self) -> None:
        run = self._run
        if run is None or run["status"] in _RUN_DONE:
            return
        if self._awaiting in {"action_approval", "action_run"}:
            self._mark_step_locked("failed")
            self._finish_locked("failed", "The approved browser action expired.")
            self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow action expired")

    def _confirm_observe_locked(self, action_id: str, approved: object) -> dict:
        pending = self._pending
        run = self._run
        if pending is None or run is None or pending["action_id"] != action_id or self._awaiting != "observe":
            self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow step rejected")
            return self._stop_view("That action is not waiting for approval.")
        if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
            self._consume_locked(action_id)
            self._finish_locked("stopped", "The workflow was cancelled.")
            return self._view_locked(False)
        if type(approved) is not bool:
            return self._stop_view("That action is not waiting for approval.")
        if not approved:
            self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)
            self._mark_step_locked("denied")
            self._finish_locked("denied", "The action was denied.")
            self._audit.write(status="browser_workflow_denied", outcome="denied", detail="Browser workflow step denied")
            return self._view_locked(False)
        if not self._enabled_flag:
            self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)
            self._finish_locked("failed", "Browser workflow is not enabled.")
            return self._view_locked(False)
        if not self._halt.resolve_approval(action_id, "approved"):
            self._consume_locked(action_id)
            self._finish_locked("stopped", "The workflow was cancelled.")
            return self._view_locked(False)
        generation = pending["generation"]
        deadline = pending["expires"]
        self._consume_locked(action_id)
        if self._clock() >= deadline or not self._halt.is_current(generation):
            self._finish_locked("failed", "That approval has expired." if self._halt.is_current(generation) else "The workflow was cancelled.")
            return self._view_locked(False)
        try:
            self._require_fresh_targets_locked(require_elements=False)
        except _WorkflowError as exc:
            self._mark_step_locked("failed")
            self._finish_locked("failed", exc.text)
            self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow observe failed")
            return self._view_locked(False)
        self._mark_step_locked("completed")
        self._audit.write(status="browser_workflow_step", outcome="completed", detail="Browser workflow observe completed")
        if run["index"] + 1 >= len(run["steps"]):
            self._finish_locked("completed", "The browser workflow finished.")
            self._audit.write(status="browser_workflow_completed", outcome="completed", detail="Browser workflow finished")
            return self._view_locked(True)
        run["index"] += 1
        run["status"] = "waiting"
        self._awaiting = ""
        try:
            self._arm_step_locked()
        except _WorkflowError as exc:
            self._finish_locked("failed", exc.text)
            self._audit.write(status="browser_workflow_failed", outcome="failed", detail="Browser workflow failed")
            return self._view_locked(False)
        return self._view_locked(True)

    def _confirm_action_locked(self, action_id: str, approved: object) -> dict:
        run = self._run
        if run is None or run["status"] in _RUN_DONE:
            return self._stop_view("That action is not waiting for approval.")
        if not self._enabled_flag:
            return self._stop_view("Browser workflow is not enabled.")
        ready = self._bridge.actions._ready
        if (
            approved is True
            and self._awaiting == "action_run"
            and ready is not None
            and ready.get("action_id") == action_id
        ):
            return self._view_locked(True)
        if type(approved) is not bool:
            return self._stop_view("That action is not waiting for approval.")
        if not self._bridge.actions._enabled_flag:
            self._finish_locked("failed", "Browser actions are not enabled.")
            return self._view_locked(False)
        expected_id = ""
        if self._bridge.actions._pending is not None:
            expected_id = self._bridge.actions._pending["action_id"]
        elif run["steps"][run["index"]].get("action_id"):
            expected_id = run["steps"][run["index"]]["action_id"]
        if expected_id and action_id != expected_id and approved is True:
            self._audit.write(status="browser_workflow_rejected", outcome="rejected", detail="Browser workflow step rejected")
            return self._stop_view("That action is not waiting for approval.")
        view = self._bridge.actions.confirm(action_id, approved)
        if not approved:
            if expected_id and action_id != expected_id:
                return self._stop_view("That action is not waiting for approval.")
            self._mark_step_locked("denied")
            self._finish_locked("denied", "The action was denied.")
            self._awaiting = ""
            self._audit.write(status="browser_workflow_denied", outcome="denied", detail="Browser workflow step denied")
            return self._view_locked(False)
        if view.get("ready") is None:
            # Another thread may have already moved this step to action_run.
            ready = self._bridge.actions._ready
            if self._awaiting == "action_run" and ready is not None and ready.get("action_id") == action_id:
                return self._view_locked(True)
            if (
                self._run is not None
                and self._run["status"] not in _RUN_DONE
                and self._awaiting == "action_approval"
                and action_id == expected_id
            ):
                self._mark_step_locked("failed")
                self._finish_locked("failed", view.get("text") or "The browser action was not approved.")
                self._awaiting = ""
            return self._view_locked(False)
        run["steps"][run["index"]]["action_id"] = action_id
        run["steps"][run["index"]]["state"] = "ready"
        run["status"] = "waiting"
        self._awaiting = "action_run"
        self._used.add(action_id)
        self._notice = "Approved. Run the action from the extension popup."
        self._audit.write(status="browser_workflow_ready", outcome="pending", detail="Browser workflow step ready")
        return self._view_locked(True)

    def _prepare_step_locked(self, step: dict) -> dict:
        action = step["action"]
        if action == "observe":
            if step["element_id"] or step["text"]:
                raise _WorkflowError("That browser workflow step is not valid.")
            return {
                "action": action,
                "summary": "Observe the active page targets.",
                "element_id": "",
                "text": "",
                "label": "",
                "state": "pending",
                "action_id": "",
            }
        targets = self._bridge.actions._targets
        if targets is None or not self._halt.is_current(targets["generation"]) or self._clock() >= targets["expires"]:
            raise _WorkflowError("Scan page targets before proposing a workflow.")
        element = targets["elements"].get(step["element_id"])
        if element is None:
            raise _WorkflowError("That page target is not available.")
        if action == "click" and element["kind"] != "button":
            raise _WorkflowError("That target cannot be clicked.")
        if action in {"focus", "type", "select_all"} and element["kind"] != "text_field":
            raise _WorkflowError("That target is not a text field.")
        if action == "type":
            if not _TEXT.fullmatch(step["text"]) or looks_like_secret(step["text"]):
                raise _WorkflowError("That text is not allowed.")
        elif step["text"]:
            raise _WorkflowError("That browser workflow step is not valid.")
        summary = {
            "click": f"Click {element['label']}.",
            "focus": f"Focus {element['label']}.",
            "select_all": f"Select all text in {element['label']}.",
            "type": f"Type into {element['label']}.",
        }[action]
        return {
            "action": action,
            "summary": summary,
            "element_id": element["element_id"],
            "fingerprint": element["fingerprint"],
            "text": step["text"] if action == "type" else "",
            "label": element["label"],
            "state": "pending",
            "action_id": "",
        }

    def _arm_step_locked(self) -> None:
        run = self._run
        if run is None:
            raise _WorkflowError("That browser workflow is not waiting.")
        if not self._halt.is_current(run["generation"]):
            raise _WorkflowError("The workflow was cancelled.")
        step = run["steps"][run["index"]]
        step["state"] = "waiting"
        run["status"] = "waiting"
        if step["action"] == "observe":
            self._require_fresh_targets_locked(require_elements=False)
            action_id = uuid.uuid4().hex
            if action_id in self._used or self._halt.approval_state(action_id) is not None:
                raise _WorkflowError("That action is not waiting for approval.")
            try:
                self._halt.request_approval(action_id)
            except HaltedError as exc:
                self._used.add(action_id)
                raise _WorkflowError("The workflow was cancelled.") from exc
            self._pending = {
                "action_id": action_id,
                "action": "observe",
                "generation": run["generation"],
                "expires": self._clock() + self._ttl_s,
                "summary": step["summary"],
            }
            self._awaiting = "observe"
            self._notice = step["summary"]
            self._audit.write(status="browser_workflow_preview", outcome="pending", detail="Browser workflow step previewed")
            return
        if not self._bridge.actions._enabled_flag:
            raise _WorkflowError("Browser actions are not enabled.")
        self._require_fresh_targets_locked(require_elements=True)
        targets = self._bridge.actions._targets
        element = targets["elements"].get(step["element_id"]) if targets else None
        if element is None:
            raise _WorkflowError("Scan page targets again before this step.")
        if step.get("fingerprint") and element["fingerprint"] != step["fingerprint"]:
            raise _WorkflowError("The page target no longer matches.")
        # Refresh fingerprint binding for this step from the latest scan.
        step["fingerprint"] = element["fingerprint"]
        step["label"] = element["label"]
        view = self._bridge.actions.propose(
            {
                "action": step["action"],
                "element_id": step["element_id"],
                "text": step["text"],
            },
            for_workflow=True,
        )
        pending = view.get("pending")
        if not pending or not pending.get("action_id"):
            raise _WorkflowError(view.get("text") or "The browser action could not be proposed.")
        step["action_id"] = pending["action_id"]
        self._awaiting = "action_approval"
        self._notice = pending.get("summary") or step["summary"]
        self._audit.write(status="browser_workflow_preview", outcome="pending", detail="Browser workflow step previewed")

    def _require_fresh_targets_locked(self, *, require_elements: bool) -> None:
        session = self._bridge.session_snapshot()
        if session is None:
            raise _WorkflowError("Connect the browser bridge first.")
        run = self._run
        if run is not None and session["session_id"] != run["session_id"]:
            raise _WorkflowError("The browser session changed.")
        targets = self._bridge.actions._targets
        if targets is None or not self._halt.is_current(targets["generation"]) or self._clock() >= targets["expires"]:
            raise _WorkflowError("Scan page targets before continuing.")
        if targets["session_id"] != session["session_id"]:
            raise _WorkflowError("Scan page targets again for the current session.")
        if require_elements and not targets["elements"]:
            raise _WorkflowError("Scan page targets before continuing.")

    def _mark_step_locked(self, state: str) -> None:
        run = self._run
        if run is None:
            return
        if 0 <= run["index"] < len(run["steps"]):
            run["steps"][run["index"]]["state"] = state

    def _finish_locked(self, status: str, notice: str) -> None:
        run = self._run
        if run is not None:
            run["status"] = status
            for index, step in enumerate(run["steps"]):
                if index < run["index"]:
                    continue
                if step["state"] in {"completed", "failed", "denied", "stopped", "cancelled", "skipped"}:
                    continue
                if index == run["index"] and status in {"failed", "denied", "stopped", "cancelled"}:
                    step["state"] = status
                elif step["state"] in {"pending", "waiting", "ready"}:
                    step["state"] = "skipped"
        self._awaiting = ""
        self._pending = None
        self._notice = notice
        self._checkpoint_locked()

    def _checkpoint_locked(self) -> None:
        if self._recovery is None or self._run is None:
            return
        try:
            snapshot = dict(self._run)
            snapshot["awaiting"] = self._awaiting
            snapshot["pending"] = self._pending is not None or self._awaiting in {"action_approval", "action_run", "observe"}
            self._recovery.record_browser(snapshot)
        except Exception:
            pass

    def _cancel_pending_locked(self) -> None:
        pending = self._pending
        if pending is not None:
            action_id = pending["action_id"]
            if self._halt.approval_state(action_id) == "pending":
                self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)

    def _consume_locked(self, action_id: str) -> None:
        self._used.add(action_id)
        if self._pending is not None and self._pending["action_id"] == action_id:
            self._pending = None

    def _expire_locked(self) -> str | None:
        expired = None
        pending = self._pending
        if pending is not None and (not self._halt.is_current(pending["generation"]) or self._clock() >= pending["expires"]):
            action_id = pending["action_id"]
            if self._halt.approval_state(action_id) == "pending":
                self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._mark_step_locked("failed")
                self._finish_locked(
                    "failed",
                    "That approval has expired." if self._halt.is_current(pending["generation"]) else "The workflow was cancelled.",
                )
            expired = action_id
        run = self._run
        if run is not None and run["status"] not in _RUN_DONE:
            if not self._halt.is_current(run["generation"]) or self._clock() >= run["expires"]:
                self._cancel_pending_locked()
                self._finish_locked(
                    "stopped" if not self._halt.is_current(run["generation"]) else "failed",
                    "The workflow was cancelled." if not self._halt.is_current(run["generation"]) else "The workflow expired.",
                )
        return expired

    def _stop_view(self, text: str) -> dict:
        self._notice = text
        return self._view_locked(False)

    def _view_locked(self, performed: bool) -> dict:
        pending = None
        if self._awaiting == "observe" and self._pending is not None:
            pending = {
                "action_id": self._pending["action_id"],
                "action": "observe",
                "summary": self._pending["summary"],
                "text": "",
                "state": "waiting for approval",
            }
        elif self._awaiting == "action_approval":
            action_pending = self._bridge.actions._pending
            if action_pending is not None:
                pending = {
                    "action_id": action_pending["action_id"],
                    "action": action_pending["action"],
                    "summary": action_pending["summary"],
                    "text": action_pending.get("text") or "",
                    "label": action_pending.get("label") or "",
                    "state": "waiting for approval",
                }
        ready = None
        if self._awaiting == "action_run":
            action_ready = self._bridge.actions._ready
            if action_ready is not None:
                ready = {
                    "action_id": action_ready["action_id"],
                    "action": action_ready["action"],
                    "summary": action_ready["summary"],
                    "state": "approved — run from the extension",
                }
        run = None
        if self._run is not None:
            run = {
                "plan_id": self._run["plan_id"],
                "status": self._run["status"],
                "index": self._run["index"],
                "steps": [
                    {
                        "action": step["action"],
                        "summary": step["summary"],
                        "label": step.get("label") or "",
                        "state": step["state"],
                        "has_text": bool(step.get("text")),
                    }
                    for step in self._run["steps"]
                ],
            }
        return {
            "ok": self._pending is not None or performed or (self._run is not None and self._run["status"] == "completed"),
            "permission_enabled": self._enabled_flag,
            "text": self._notice,
            "pending": pending,
            "ready": ready,
            "run": run,
            "awaiting": self._awaiting,
            "performed": performed,
            "executed": bool(self._run and self._run["status"] == "completed"),
        }


def _steps(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise _WorkflowError("That browser workflow is not valid.")
    if set(payload) - {"steps"}:
        raise _WorkflowError("That browser workflow is not valid.")
    rows = payload.get("steps")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_STEPS:
        raise _WorkflowError("That browser workflow is not valid.")
    steps = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"action", "element_id", "text"}:
            raise _WorkflowError("That browser workflow step is not valid.")
        action = row.get("action")
        if action not in _ACTIONS:
            raise _WorkflowError("That browser workflow action is not allowed.")
        element_id = row.get("element_id", "")
        text = row.get("text", "")
        if action == "observe":
            if element_id not in {"", None} or text not in {"", None}:
                raise _WorkflowError("That browser workflow step is not valid.")
            steps.append({"action": action, "element_id": "", "text": ""})
            continue
        if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
            raise _WorkflowError("That page target is not available.")
        if action == "type":
            if not isinstance(text, str) or not _TEXT.fullmatch(text):
                raise _WorkflowError("That text is not allowed.")
        else:
            if text not in {"", None}:
                raise _WorkflowError("That browser workflow step is not valid.")
            text = ""
        steps.append({"action": action, "element_id": element_id, "text": text})
    return steps


def _monotonic() -> float:
    import time

    return time.monotonic()


class _WorkflowError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
