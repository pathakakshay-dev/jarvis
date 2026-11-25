"""Multi-App Workflow Orchestrator over existing approved executors.

Plans are validated locally (no AI planner). Execution arms one capability at a
time and forwards Start/Confirm into Browser Workflow, Desktop Action Engine,
Notepad/Calculator openers, or System Info — never bypassing their approvals.
"""

from __future__ import annotations

import re
import threading
import uuid

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_MAX_STEPS = 6
_ID = re.compile(r"^[0-9a-f]{32}$")
_ELEMENT_ID = re.compile(r"^[0-9a-f]{16}$")
_TEXT = re.compile(r"^[A-Za-z0-9 .,!?'\-]{1,200}$")
_CAPABILITIES = frozenset(
    {
        "system_info",
        "open_notepad",
        "open_calculator",
        "desktop_engine",
        "browser_workflow",
    }
)
_BROWSER_ACTIONS = frozenset({"observe", "click", "focus", "type", "select_all"})
_DESKTOP_ACTIONS = frozenset({"observe", "focus_window", "type_text", "click_element", "key_shortcut"})
_DESKTOP_SHORTCUTS = frozenset({"ctrl+a"})
_RUN_DONE = frozenset({"completed", "failed", "stopped", "cancelled", "denied"})
_APP_LABELS = {
    "system_info": "System Information",
    "open_notepad": "Notepad",
    "open_calculator": "Calculator",
    "desktop_engine": "Desktop Action Engine",
    "browser_workflow": "Browser Workflow",
}


class MultiAppOrchestrator:
    """Propose a short multi-app plan, Start it, and approve each underlying action."""

    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        skill_executor,
        notepad,
        calculator,
        desktop_engine,
        browser_bridge,
        clock=None,
        ttl_s: float = _TTL_S,
        recovery=None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid multi-app workflow lifetime")
        self._halt = halt
        self._audit = audit
        self._skills = skill_executor
        self._notepad = notepad
        self._calculator = calculator
        self._desktop = desktop_engine
        self._bridge = browser_bridge
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._recovery = recovery
        self._enabled_flag = False
        self._lock = threading.RLock()
        self._run: dict | None = None
        self._expect: dict | None = None
        self._used: set[str] = set()
        self._notice = ""
        self._phase = ""  # "", "underlying_start", "underlying_confirm", "browser_run"

    def status(self) -> dict:
        self._sync_underlying()
        with self._lock:
            self._expire_locked()
            return self._view_locked(False)

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled_flag = bool(enabled)
            if not self._enabled_flag:
                self._cancel_expect_locked()
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._finish_locked("cancelled", "Multi-app workflow is off.")
                else:
                    self._notice = "Multi-app workflow is off."
            else:
                self._notice = "Multi-app workflow is on. Propose a short plan across supported apps."
            return self._enabled_flag

    def busy(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._expect is not None or (self._run is not None and self._run["status"] not in _RUN_DONE)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._expect is not None and not self._halt.is_current(self._expect["generation"])):
                self._cancel_expect_locked()
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._run["status"] = "stopped"
                    for step in self._run["steps"][self._run["index"] :]:
                        if step["state"] in {"pending", "waiting", "ready", "running"}:
                            step["state"] = "stopped"
                    self._checkpoint_locked()
                self._phase = ""
                self._notice = ""

    def clear(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._cancel_expect_locked()
            self._run = None
            self._phase = ""
            self._notice = "Multi-app workflow state was cleared."
            self._audit.write(status="multi_app_cleared", outcome="completed", detail="Multi-app workflow cleared")
            return self._view_locked(False)

    def cancel(self) -> dict:
        targets = self._cancel_targets()
        self._cancel_underlying(targets)
        with self._lock:
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._finish_locked("cancelled", "The multi-app workflow was cancelled.")
            else:
                self._notice = "The multi-app workflow was cancelled."
            self._phase = ""
            self._audit.write(status="multi_app_cancelled", outcome="cancelled", detail="Multi-app workflow cancelled")
            return self._view_locked(False)

    def propose(self, payload: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The multi-app workflow was cancelled.")
            if not self._enabled_flag:
                self._audit.write(status="multi_app_rejected", outcome="rejected", detail="Multi-app workflow is not enabled")
                return self._stop_view("Multi-app workflow is not enabled.")
            if self._expect is not None or (self._run is not None and self._run["status"] not in _RUN_DONE):
                return self._stop_view("A multi-app workflow is already running.")
            try:
                steps = _steps(payload)
            except _MultiAppError as exc:
                self._audit.write(status="multi_app_rejected", outcome="rejected", detail="Multi-app workflow rejected")
                return self._stop_view(exc.text)
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._stop_view("The multi-app workflow was cancelled.")
            plan_id = uuid.uuid4().hex
            apps = []
            for step in steps:
                label = _APP_LABELS[step["capability"]]
                if label not in apps:
                    apps.append(label)
            self._run = {
                "plan_id": plan_id,
                "generation": generation,
                "status": "proposed",
                "index": 0,
                "steps": steps,
                "apps": apps,
                "expires": self._clock() + self._ttl_s,
            }
            self._phase = ""
            self._notice = "Review the multi-app workflow and affected apps."
            self._audit.write(status="multi_app_proposed", outcome="pending", detail="Multi-app workflow proposed")
            self._checkpoint_locked()
            return self._view_locked(False)

    def start(self, plan_id: object, approved: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The multi-app workflow was cancelled.")
            if not self._enabled_flag:
                return self._stop_view("Multi-app workflow is not enabled.")
            run = self._run
            if run is None or not isinstance(plan_id, str) or plan_id != run["plan_id"]:
                return self._stop_view("That multi-app workflow is not waiting.")
            if run["status"] != "proposed":
                return self._view_locked(False)
            if type(approved) is not bool or not approved:
                self._finish_locked("denied", "The multi-app workflow was denied.")
                self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app workflow denied")
                return self._view_locked(False)
            if not self._halt.is_current(run["generation"]):
                self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                return self._view_locked(False)
            self._audit.write(status="multi_app_started", outcome="pending", detail="Multi-app workflow started")
        view = self._arm_current()
        with self._lock:
            self._checkpoint_locked()
        return view

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id):
                self._audit.write(status="multi_app_rejected", outcome="rejected", detail="Multi-app step rejected")
                return self._stop_view("That action is not waiting for approval.")
            if action_id in self._used:
                if self._phase == "browser_run" or (self._run is not None and self._run["status"] in {"waiting", "running", "completed"}):
                    return self._view_locked(True)
                self._audit.write(status="multi_app_rejected", outcome="rejected", detail="Multi-app step rejected")
                return self._stop_view("That action is not waiting for approval.")
            expect = self._expect
            run = self._run
            if expect is None or run is None or expect["action_id"] != action_id:
                self._audit.write(status="multi_app_rejected", outcome="rejected", detail="Multi-app step rejected")
                return self._stop_view("That action is not waiting for approval.")
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(expect["generation"]):
                self._consume_expect_locked(action_id)
                self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                return self._view_locked(False)
            if not self._enabled_flag:
                self._consume_expect_locked(action_id)
                self._finish_locked("failed", "Multi-app workflow is not enabled.")
                return self._view_locked(False)

            kind = expect["kind"]
            capability = expect["capability"]
            plan_ref = expect.get("plan_id") or ""
            generation = expect["generation"]
            deadline = expect["expires"]

            if kind == "underlying_start":
                if self._halt.approval_state(action_id) != "pending":
                    self._consume_expect_locked(action_id)
                    self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                    return self._view_locked(False)
                if not approved:
                    self._halt.resolve_approval(action_id, "denied")
                    self._consume_expect_locked(action_id)
                    self._mark_step_locked("denied")
                    self._finish_locked("denied", "The action was denied.")
                    self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app step denied")
                    return self._view_locked(False)
                if not self._halt.resolve_approval(action_id, "approved"):
                    self._consume_expect_locked(action_id)
                    self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                    return self._view_locked(False)
                self._consume_expect_locked(action_id)
                if self._clock() >= deadline or not self._halt.is_current(generation):
                    self._finish_locked(
                        "failed",
                        "That approval has expired." if self._halt.is_current(generation) else "The multi-app workflow was cancelled.",
                    )
                    return self._view_locked(False)
            else:
                # Nested confirm: the action_id belongs to the underlying executor.
                if self._halt.approval_state(action_id) != "pending":
                    self._consume_expect_locked(action_id)
                    self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                    return self._view_locked(False)
                if self._clock() >= deadline:
                    self._consume_expect_locked(action_id)
                    self._finish_locked("failed", "That approval has expired.")
                    return self._view_locked(False)
                self._consume_expect_locked(action_id)

        try:
            if kind == "underlying_start":
                return self._forward_start(capability, plan_ref, generation)
            return self._forward_confirm(capability, action_id, approved, generation)
        except _MultiAppError as exc:
            with self._lock:
                self._mark_step_locked("failed")
                self._finish_locked("failed", exc.text)
                self._audit.write(status="multi_app_failed", outcome="failed", detail="Multi-app step failed")
                return self._view_locked(False)

    def _arm_current(self) -> dict:
        with self._lock:
            run = self._run
            if run is None or run["status"] in _RUN_DONE:
                return self._view_locked(False)
            if not self._enabled_flag:
                self._finish_locked("failed", "Multi-app workflow is not enabled.")
                return self._view_locked(False)
            if not self._halt.is_current(run["generation"]):
                self._finish_locked("stopped", "The multi-app workflow was cancelled.")
                return self._view_locked(False)
            step = run["steps"][run["index"]]
            step["state"] = "waiting"
            run["status"] = "waiting"
            capability = step["capability"]
            payload = step.get("payload") or {}

        try:
            if capability == "system_info":
                return self._arm_system()
            if capability == "open_notepad":
                return self._arm_launcher(self._notepad, "open_notepad", "Open Notepad.")
            if capability == "open_calculator":
                return self._arm_launcher(self._calculator, "open_calculator", "Open Calculator.")
            if capability == "desktop_engine":
                return self._arm_desktop(payload)
            if capability == "browser_workflow":
                return self._arm_browser(payload)
            raise _MultiAppError("That multi-app capability is not allowed.")
        except _MultiAppError as exc:
            with self._lock:
                self._mark_step_locked("failed")
                self._finish_locked("failed", exc.text)
                self._audit.write(status="multi_app_failed", outcome="failed", detail="Multi-app arm failed")
                return self._view_locked(False)

    def _arm_system(self) -> dict:
        result = self._skills.submit("system_info", {})
        if result.outcome == "disabled":
            raise _MultiAppError("System information is not enabled.")
        if result.outcome != "needs_approval" or not result.action_id:
            raise _MultiAppError(result.text or "System information could not be requested.")
        return self._expect_nested("system_info", result.action_id, "Read system information.")

    def _arm_launcher(self, opener, capability: str, summary: str) -> dict:
        view = opener.request()
        pending = view.get("pending") if isinstance(view, dict) else None
        if not pending or not pending.get("action_id"):
            raise _MultiAppError((view or {}).get("detail") or f"{summary} could not be requested.")
        return self._expect_nested(capability, pending["action_id"], summary)

    def _arm_desktop(self, payload: dict) -> dict:
        if self._desktop_busy():
            raise _MultiAppError("A desktop task is already running.")
        view = self._desktop.propose({"steps": [payload]})
        task = view.get("task") if isinstance(view, dict) else None
        if not task or task.get("status") != "proposed" or not task.get("plan_id"):
            raise _MultiAppError((view or {}).get("text") or "The desktop task could not be proposed.")
        summary = (task.get("steps") or [{}])[0].get("summary") or "Start the desktop action."
        return self._expect_start("desktop_engine", task["plan_id"], summary)

    def _arm_browser(self, payload: dict) -> dict:
        workflow = self._bridge.workflow
        if workflow.busy():
            raise _MultiAppError("A browser workflow is already running.")
        view = workflow.propose({"steps": [payload]})
        run = view.get("run") if isinstance(view, dict) else None
        if not run or run.get("status") != "proposed" or not run.get("plan_id"):
            raise _MultiAppError((view or {}).get("text") or "The browser workflow could not be proposed.")
        summary = (run.get("steps") or [{}])[0].get("summary") or "Start the browser workflow step."
        return self._expect_start("browser_workflow", run["plan_id"], summary)

    def _expect_start(self, capability: str, plan_id: str, summary: str) -> dict:
        with self._lock:
            run = self._run
            if run is None or run["status"] in _RUN_DONE:
                raise _MultiAppError("That multi-app workflow is not waiting.")
            if not self._halt.is_current(run["generation"]):
                raise _MultiAppError("The multi-app workflow was cancelled.")
            action_id = uuid.uuid4().hex
            if action_id in self._used or self._halt.approval_state(action_id) is not None:
                raise _MultiAppError("That action is not waiting for approval.")
            try:
                self._halt.request_approval(action_id)
            except Exception as exc:
                self._used.add(action_id)
                raise _MultiAppError("The multi-app workflow was cancelled.") from exc
            self._expect = {
                "kind": "underlying_start",
                "action_id": action_id,
                "capability": capability,
                "plan_id": plan_id,
                "summary": summary,
                "generation": run["generation"],
                "expires": self._clock() + self._ttl_s,
            }
            self._phase = "underlying_start"
            step = run["steps"][run["index"]]
            step["state"] = "waiting"
            step["summary"] = summary
            run["status"] = "waiting"
            self._notice = summary
            self._audit.write(status="multi_app_preview", outcome="pending", detail="Multi-app step previewed")
            return self._view_locked(False)

    def _expect_nested(self, capability: str, nested_id: str, summary: str) -> dict:
        with self._lock:
            run = self._run
            if run is None or run["status"] in _RUN_DONE:
                raise _MultiAppError("That multi-app workflow is not waiting.")
            if not self._halt.is_current(run["generation"]):
                raise _MultiAppError("The multi-app workflow was cancelled.")
            if not isinstance(nested_id, str) or not _ID.fullmatch(nested_id):
                raise _MultiAppError("That action is not waiting for approval.")
            if nested_id in self._used:
                raise _MultiAppError("That action is not waiting for approval.")
            self._expect = {
                "kind": "underlying_confirm",
                "action_id": nested_id,
                "capability": capability,
                "plan_id": "",
                "summary": summary,
                "generation": run["generation"],
                "expires": self._clock() + self._ttl_s,
            }
            self._phase = "underlying_confirm"
            step = run["steps"][run["index"]]
            step["state"] = "waiting"
            step["summary"] = summary
            run["status"] = "waiting"
            self._notice = summary
            self._audit.write(status="multi_app_preview", outcome="pending", detail="Multi-app step previewed")
            return self._view_locked(False)

    def _forward_start(self, capability: str, plan_id: str, generation: int) -> dict:
        if not self._halt.is_current(generation):
            raise _MultiAppError("The multi-app workflow was cancelled.")
        if capability == "desktop_engine":
            view = self._desktop.start(plan_id, True)
            pending = view.get("pending") if isinstance(view, dict) else None
            task = view.get("task") if isinstance(view, dict) else None
            if task and task.get("status") in _RUN_DONE and task.get("status") != "completed":
                raise _MultiAppError(view.get("text") or "The desktop task failed.")
            if not pending or not pending.get("action_id"):
                if task and task.get("status") == "completed":
                    return self._complete_step(view.get("text") or "Desktop step completed.")
                raise _MultiAppError(view.get("text") or "The desktop task could not be started.")
            summary = pending.get("summary") or "Approve the desktop action."
            return self._expect_nested("desktop_engine", pending["action_id"], summary)
        if capability == "browser_workflow":
            view = self._bridge.workflow.start(plan_id, True)
            return self._after_browser_view(view)
        raise _MultiAppError("That multi-app capability is not allowed.")

    def _forward_confirm(self, capability: str, action_id: str, approved: bool, generation: int) -> dict:
        if not self._halt.is_current(generation):
            raise _MultiAppError("The multi-app workflow was cancelled.")
        if capability == "system_info":
            result = self._skills.confirm(action_id, approved=approved)
            if not approved or result.outcome == "denied":
                with self._lock:
                    self._mark_step_locked("denied")
                    self._finish_locked("denied", result.text or "The action was denied.")
                    self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app step denied")
                    return self._view_locked(False)
            if result.outcome == "completed":
                return self._complete_step("System information was read.")
            raise _MultiAppError(result.text or "System information failed.")
        if capability == "open_notepad":
            view = self._notepad.confirm(action_id, approved)
            return self._after_launcher(view, "Notepad was opened.", "Notepad was not opened.")
        if capability == "open_calculator":
            view = self._calculator.confirm(action_id, approved)
            return self._after_launcher(view, "Calculator was opened.", "Calculator was not opened.")
        if capability == "desktop_engine":
            view = self._desktop.confirm(action_id, approved)
            pending = view.get("pending") if isinstance(view, dict) else None
            task = view.get("task") if isinstance(view, dict) else None
            if not approved or (task and task.get("status") == "denied"):
                with self._lock:
                    self._mark_step_locked("denied")
                    self._finish_locked("denied", (view or {}).get("text") or "The action was denied.")
                    self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app step denied")
                    return self._view_locked(False)
            if pending and pending.get("action_id"):
                summary = pending.get("summary") or "Approve the next desktop action."
                return self._expect_nested("desktop_engine", pending["action_id"], summary)
            if task and task.get("status") == "completed":
                return self._complete_step(view.get("text") or "Desktop step completed.")
            raise _MultiAppError((view or {}).get("text") or "The desktop action failed.")
        if capability == "browser_workflow":
            view = self._bridge.workflow.confirm(action_id, approved)
            if not approved:
                with self._lock:
                    self._mark_step_locked("denied")
                    self._finish_locked("denied", (view or {}).get("text") or "The action was denied.")
                    self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app step denied")
                    return self._view_locked(False)
            return self._after_browser_view(view)
        raise _MultiAppError("That multi-app capability is not allowed.")

    def _after_launcher(self, view: dict, ok_text: str, fail_text: str) -> dict:
        if view.get("launched") is True:
            return self._complete_step(ok_text)
        detail = view.get("detail") or fail_text
        if "denied" in detail.lower():
            with self._lock:
                self._mark_step_locked("denied")
                self._finish_locked("denied", detail)
                self._audit.write(status="multi_app_denied", outcome="denied", detail="Multi-app step denied")
                return self._view_locked(False)
        raise _MultiAppError(detail)

    def _after_browser_view(self, view: dict) -> dict:
        run = view.get("run") if isinstance(view, dict) else None
        pending = view.get("pending") if isinstance(view, dict) else None
        ready = view.get("ready") if isinstance(view, dict) else None
        awaiting = view.get("awaiting") if isinstance(view, dict) else ""
        if run and run.get("status") == "completed":
            return self._complete_step(view.get("text") or "Browser step completed.")
        if run and run.get("status") == "denied":
            with self._lock:
                self._mark_step_locked("denied")
                self._finish_locked("denied", view.get("text") or "The action was denied.")
                return self._view_locked(False)
        if run and run.get("status") in {"failed", "stopped", "cancelled"}:
            raise _MultiAppError(view.get("text") or "The browser workflow failed.")
        if pending and pending.get("action_id"):
            summary = pending.get("summary") or "Approve the browser action."
            return self._expect_nested("browser_workflow", pending["action_id"], summary)
        if ready and awaiting == "action_run":
            with self._lock:
                if self._run is None or self._run["status"] in _RUN_DONE:
                    return self._view_locked(False)
                self._phase = "browser_run"
                self._run["status"] = "running"
                self._run["steps"][self._run["index"]]["state"] = "running"
                self._notice = ready.get("summary") or "Run the approved browser action from the extension."
                return self._view_locked(True)
        raise _MultiAppError(view.get("text") or "The browser workflow could not continue.")

    def _complete_step(self, notice: str) -> dict:
        with self._lock:
            run = self._run
            if run is None or run["status"] in _RUN_DONE:
                return self._view_locked(False)
            self._mark_step_locked("completed")
            self._audit.write(status="multi_app_step", outcome="completed", detail="Multi-app step completed")
            if run["index"] + 1 >= len(run["steps"]):
                self._finish_locked("completed", notice if notice else "The multi-app workflow finished.")
                self._audit.write(status="multi_app_completed", outcome="completed", detail="Multi-app workflow finished")
                return self._view_locked(True)
            run["index"] += 1
            run["status"] = "waiting"
            self._phase = ""
            self._notice = notice
        return self._arm_current()

    def _sync_underlying(self) -> None:
        """Advance or fail when a browser extension run finishes. Driven by status polls, not a background worker."""
        with self._lock:
            if self._phase != "browser_run" or self._run is None or self._run["status"] in _RUN_DONE:
                return
            generation = self._run["generation"]
        if not self._halt.is_current(generation):
            with self._lock:
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._finish_locked("stopped", "The multi-app workflow was cancelled.")
            return
        view = self._bridge.workflow.status()
        run = view.get("run") if isinstance(view, dict) else None
        if run is None:
            return
        status = run.get("status")
        if status == "completed":
            self._complete_step(view.get("text") or "Browser step completed.")
            return
        if status in {"failed", "stopped", "cancelled", "denied"}:
            with self._lock:
                if self._run is None or self._run["status"] in _RUN_DONE:
                    return
                mapped = status if status in {"failed", "denied", "stopped", "cancelled"} else "failed"
                self._mark_step_locked(mapped)
                self._finish_locked(mapped if mapped in _RUN_DONE else "failed", view.get("text") or "The browser workflow failed.")
                self._audit.write(status="multi_app_failed", outcome="failed", detail="Multi-app browser step failed")

    def _desktop_busy(self) -> bool:
        view = self._desktop.status()
        if view.get("pending"):
            return True
        task = view.get("task")
        return bool(task and task.get("status") not in _RUN_DONE)

    def _cancel_targets(self) -> dict:
        with self._lock:
            self._expire_locked()
            capability = ""
            nested_id = ""
            kind = ""
            if self._expect is not None:
                capability = self._expect.get("capability") or ""
                nested_id = self._expect.get("action_id") or ""
                kind = self._expect.get("kind") or ""
            elif self._run is not None and self._run["status"] not in _RUN_DONE:
                capability = self._run["steps"][self._run["index"]]["capability"]
            self._cancel_expect_locked()
            self._phase = ""
            return {"capability": capability, "nested_id": nested_id, "kind": kind}

    def _cancel_underlying(self, targets: dict) -> None:
        capability = targets.get("capability") or ""
        nested_id = targets.get("nested_id") or ""
        kind = targets.get("kind") or ""
        try:
            if capability == "desktop_engine":
                self._desktop.cancel()
            elif capability == "browser_workflow":
                self._bridge.workflow.cancel()
            elif capability == "open_notepad" and kind == "underlying_confirm" and nested_id:
                self._notepad.confirm(nested_id, False)
            elif capability == "open_calculator" and kind == "underlying_confirm" and nested_id:
                self._calculator.confirm(nested_id, False)
            elif capability == "system_info" and kind == "underlying_confirm" and nested_id:
                self._skills.confirm(nested_id, approved=False)
        except Exception:
            pass

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
                elif step["state"] in {"pending", "waiting", "ready", "running"}:
                    step["state"] = "skipped"
        self._phase = ""
        self._expect = None
        self._notice = notice
        self._checkpoint_locked()

    def _checkpoint_locked(self) -> None:
        if self._recovery is None or self._run is None:
            return
        try:
            snapshot = dict(self._run)
            snapshot["awaiting"] = self._phase
            snapshot["pending"] = self._expect is not None
            self._recovery.record_multi_app(snapshot)
        except Exception:
            pass

    def _cancel_expect_locked(self) -> None:
        expect = self._expect
        if expect is None:
            return
        action_id = expect["action_id"]
        if expect.get("kind") == "underlying_start" and self._halt.approval_state(action_id) == "pending":
            self._halt.resolve_approval(action_id, "denied")
        self._consume_expect_locked(action_id)

    def _consume_expect_locked(self, action_id: str) -> None:
        self._used.add(action_id)
        if self._expect is not None and self._expect["action_id"] == action_id:
            self._expect = None

    def _expire_locked(self) -> str | None:
        expired = None
        expect = self._expect
        if expect is not None and (not self._halt.is_current(expect["generation"]) or self._clock() >= expect["expires"]):
            action_id = expect["action_id"]
            if expect.get("kind") == "underlying_start" and self._halt.approval_state(action_id) == "pending":
                self._halt.resolve_approval(action_id, "denied")
            self._consume_expect_locked(action_id)
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._mark_step_locked("failed")
                self._finish_locked(
                    "failed",
                    "That approval has expired." if self._halt.is_current(expect["generation"]) else "The multi-app workflow was cancelled.",
                )
            expired = action_id
        run = self._run
        if run is not None and run["status"] not in _RUN_DONE:
            if not self._halt.is_current(run["generation"]) or self._clock() >= run["expires"]:
                self._cancel_expect_locked()
                self._finish_locked(
                    "stopped" if not self._halt.is_current(run["generation"]) else "failed",
                    "The multi-app workflow was cancelled." if not self._halt.is_current(run["generation"]) else "The multi-app workflow expired.",
                )
        return expired

    def _stop_view(self, text: str) -> dict:
        self._notice = text
        return self._view_locked(False)

    def _view_locked(self, performed: bool) -> dict:
        pending = None
        if self._expect is not None:
            pending = {
                "action_id": self._expect["action_id"],
                "capability": self._expect["capability"],
                "phase": self._expect["kind"],
                "summary": self._expect["summary"],
                "state": "waiting for approval",
            }
        run = None
        if self._run is not None:
            run = {
                "plan_id": self._run["plan_id"],
                "status": self._run["status"],
                "index": self._run["index"],
                "apps": list(self._run["apps"]),
                "steps": [
                    {
                        "capability": step["capability"],
                        "summary": step.get("summary") or _default_summary(step),
                        "state": step["state"],
                        "has_text": bool((step.get("payload") or {}).get("text")),
                    }
                    for step in self._run["steps"]
                ],
            }
        return {
            "ok": self._expect is not None or performed or (self._run is not None and self._run["status"] == "completed"),
            "permission_enabled": self._enabled_flag,
            "text": self._notice,
            "pending": pending,
            "run": run,
            "phase": self._phase,
            "performed": performed,
            "executed": bool(self._run and self._run["status"] == "completed"),
        }


def _default_summary(step: dict) -> str:
    capability = step["capability"]
    payload = step.get("payload") or {}
    if capability == "system_info":
        return "Read system information."
    if capability == "open_notepad":
        return "Open Notepad."
    if capability == "open_calculator":
        return "Open Calculator."
    if capability == "desktop_engine":
        return "Desktop: " + str(payload.get("action", "action")) + "."
    if capability == "browser_workflow":
        return "Browser: " + str(payload.get("action", "action")) + "."
    return "Multi-app step."


def _steps(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise _MultiAppError("That multi-app workflow is not valid.")
    if set(payload) - {"steps"}:
        raise _MultiAppError("That multi-app workflow is not valid.")
    rows = payload.get("steps")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_STEPS:
        raise _MultiAppError("That multi-app workflow is not valid.")
    steps = []
    for row in rows:
        if not isinstance(row, dict):
            raise _MultiAppError("That multi-app step is not valid.")
        capability = row.get("capability")
        if capability not in _CAPABILITIES:
            raise _MultiAppError("That multi-app capability is not allowed.")
        if capability in {"system_info", "open_notepad", "open_calculator"}:
            if set(row) - {"capability"}:
                raise _MultiAppError("That multi-app step is not valid.")
            item = {"capability": capability, "payload": {}, "summary": "", "state": "pending"}
            item["summary"] = _default_summary(item)
            steps.append(item)
            continue
        if capability == "desktop_engine":
            if set(row) - {"capability", "action", "window_id", "element_id", "text", "shortcut"}:
                raise _MultiAppError("That multi-app step is not valid.")
            nested = _desktop_payload(row)
            item = {"capability": capability, "payload": nested, "summary": "", "state": "pending"}
            item["summary"] = _default_summary(item)
            steps.append(item)
            continue
        if capability == "browser_workflow":
            if set(row) - {"capability", "action", "element_id", "text"}:
                raise _MultiAppError("That multi-app step is not valid.")
            nested = _browser_payload(row)
            item = {"capability": capability, "payload": nested, "summary": "", "state": "pending"}
            item["summary"] = _default_summary(item)
            steps.append(item)
            continue
        raise _MultiAppError("That multi-app capability is not allowed.")
    return steps


def _desktop_payload(row: dict) -> dict:
    action = row.get("action")
    if action not in _DESKTOP_ACTIONS:
        raise _MultiAppError("That desktop action is not allowed.")
    window_id = row.get("window_id", "")
    element_id = row.get("element_id", "")
    text = row.get("text", "")
    shortcut = row.get("shortcut", "")
    if not isinstance(window_id, str) or not isinstance(element_id, str) or not isinstance(text, str) or not isinstance(shortcut, str):
        raise _MultiAppError("That multi-app step is not valid.")
    if window_id and not _ID.fullmatch(window_id):
        raise _MultiAppError("That desktop target is not allowed.")
    if element_id and not _ID.fullmatch(element_id):
        raise _MultiAppError("That desktop target is not allowed.")
    if looks_like_secret(text) or looks_like_secret(shortcut):
        raise _MultiAppError("That multi-app step is not supported.")
    if shortcut:
        shortcut = shortcut.strip().lower()
        if shortcut and shortcut not in _DESKTOP_SHORTCUTS:
            raise _MultiAppError("That desktop action is not allowed.")
    if action == "type_text":
        if not _TEXT.fullmatch(text):
            raise _MultiAppError("That text is not allowed.")
    elif text:
        raise _MultiAppError("That multi-app step is not valid.")
    if action == "key_shortcut":
        if shortcut not in _DESKTOP_SHORTCUTS:
            raise _MultiAppError("That desktop action is not allowed.")
    elif shortcut:
        raise _MultiAppError("That multi-app step is not valid.")
    if action == "observe" and (window_id or element_id or text or shortcut):
        raise _MultiAppError("That multi-app step is not valid.")
    return {"action": action, "window_id": window_id, "element_id": element_id, "text": text, "shortcut": shortcut}


def _browser_payload(row: dict) -> dict:
    action = row.get("action")
    if action not in _BROWSER_ACTIONS:
        raise _MultiAppError("That browser workflow action is not allowed.")
    element_id = row.get("element_id", "")
    text = row.get("text", "")
    if not isinstance(element_id, str) or not isinstance(text, str):
        raise _MultiAppError("That multi-app step is not valid.")
    if looks_like_secret(text):
        raise _MultiAppError("That multi-app step is not supported.")
    if action == "observe":
        if element_id or text:
            raise _MultiAppError("That multi-app step is not valid.")
        return {"action": action, "element_id": "", "text": ""}
    if not _ELEMENT_ID.fullmatch(element_id):
        raise _MultiAppError("That page target is not available.")
    if action == "type":
        if not _TEXT.fullmatch(text):
            raise _MultiAppError("That text is not allowed.")
        return {"action": action, "element_id": element_id, "text": text}
    if text:
        raise _MultiAppError("That multi-app step is not valid.")
    return {"action": action, "element_id": element_id, "text": ""}


def _monotonic() -> float:
    import time

    return time.monotonic()


class _MultiAppError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
