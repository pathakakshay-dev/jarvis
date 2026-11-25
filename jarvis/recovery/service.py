"""User-initiated recovery for Goal, Multi-App, and Browser workflows."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jarvis.audit import AuditLog
from jarvis.recovery.store import CheckpointStore
from jarvis.safety.halt import HaltController
from jarvis.safety.secrets import looks_like_secret

_KINDS = frozenset({"goal", "multi_app", "browser_workflow"})
_ACTIVE_RUN = frozenset(
    {
        "proposed",
        "awaiting_approval",
        "waiting",
        "running",
        "started",
    }
)
_TERMINAL_RUN = frozenset(
    {
        "completed",
        "failed",
        "denied",
        "cancelled",
        "rejected",
        "stopped",
        "interrupted",
    }
)
_ID = __import__("re").compile(r"^[0-9a-f]{32}$")


class RecoveryService:
    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        db_path: Path,
        goal_agent=None,
        multi_app=None,
        browser_bridge=None,
    ) -> None:
        self._halt = halt
        self._audit = audit
        self._store = CheckpointStore(db_path)
        self._goal = goal_agent
        self._multi_app = multi_app
        self._bridge = browser_bridge
        self._lock = threading.RLock()
        self._notice = ""
        self.seal_interrupted_on_startup()

    def bind(self, *, goal_agent=None, multi_app=None, browser_bridge=None) -> None:
        if goal_agent is not None:
            self._goal = goal_agent
        if multi_app is not None:
            self._multi_app = multi_app
        if browser_bridge is not None:
            self._bridge = browser_bridge

    def seal_interrupted_on_startup(self) -> int:
        """Mark any active checkpoints as interrupted. Never auto-resumes."""
        count = 0
        now = _iso_now()
        with self._lock:
            for row in self._store.list_active():
                if self._store.mark_recovery(row["checkpoint_id"], "interrupted", status="interrupted", updated_at=now):
                    count += 1
                    self._audit.write(
                        status="recovery_interrupted",
                        outcome="completed",
                        detail="Workflow marked interrupted on startup",
                    )
            self._notice = "Interrupted workflows need review." if count else ""
        return count

    def status(self) -> dict:
        with self._lock:
            rows = []
            for row in self._store.list_recovery():
                public = _public(row)
                if public is not None:
                    rows.append(public)
            return {
                "ok": True,
                "text": self._notice,
                "interrupted": [row for row in rows if row["recovery_status"] in {"interrupted", "needs_review"}],
                "active": [row for row in rows if row["recovery_status"] == "active"],
            }

    def record_goal(self, run: dict | None) -> None:
        self._record("goal", run, _goal_payload)

    def record_multi_app(self, run: dict | None) -> None:
        self._record("multi_app", run, _multi_app_payload)

    def record_browser(self, run: dict | None) -> None:
        self._record("browser_workflow", run, _browser_payload)

    def abandon(self, checkpoint_id: object) -> dict:
        with self._lock:
            row = self._require(checkpoint_id)
            if row is None:
                return self._fail("That recovery item is not available.")
            if row["recovery_status"] not in {"interrupted", "needs_review", "active"}:
                return self._fail("That recovery item is not available.")
            snapshot = dict(row)
            self._store.mark_recovery(row["checkpoint_id"], "abandoned", status="cancelled", updated_at=_iso_now())
            self._notice = "Interrupted workflow abandoned."
            self._audit.write(status="recovery_abandoned", outcome="completed", detail="Interrupted workflow abandoned")
        # Cancel live work outside the recovery lock to avoid lock-order deadlocks.
        self._invalidate_live_approvals(snapshot)
        return self.status()

    def restart_begin(self, checkpoint_id: object) -> dict:
        """Start a brand-new execution from safe restart metadata. Never reuses approvals."""
        with self._lock:
            row = self._require(checkpoint_id)
            if row is None:
                return self._fail("That recovery item is not available.")
            if row["recovery_status"] not in {"interrupted", "needs_review"}:
                return self._fail("That recovery item is not waiting for recovery.")
            if self._halt.halted:
                return self._fail("The assistant is stopped.")
            payload = row.get("restart_payload")
            if not isinstance(payload, dict) or not payload:
                self._store.mark_recovery(row["checkpoint_id"], "needs_review", updated_at=_iso_now())
                return self._fail("That workflow cannot be restarted safely. Review and recreate it manually.")
            kind = row["kind"]
            snapshot = dict(row)
            self._store.mark_recovery(row["checkpoint_id"], "abandoned", status="cancelled", updated_at=_iso_now())

        self._invalidate_live_approvals(snapshot)
        try:
            if kind == "goal":
                result = self._restart_goal(payload)
            elif kind == "multi_app":
                result = self._restart_multi_app(payload)
            elif kind == "browser_workflow":
                result = self._restart_browser(payload)
            else:
                return self._fail("Unknown workflow kind.")
        except Exception:
            with self._lock:
                return self._fail("The workflow could not be restarted.")

        with self._lock:
            self._notice = "A new execution was proposed. Previous approvals were not reused."
            self._audit.write(status="recovery_restart", outcome="completed", detail="New workflow proposed from recovery")
            body = self.status()
            body["restart"] = {
                "kind": kind,
                "ok": bool(result.get("ok") or result.get("run") or result.get("can_start")),
                "text": result.get("text") or result.get("detail") or self._notice,
            }
            return body

    def _record(self, kind: str, run: dict | None, payload_fn) -> None:
        if not isinstance(run, dict):
            return
        plan_id = run.get("plan_id")
        if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id):
            return
        status = str(run.get("status") or "")
        now = _iso_now()
        with self._lock:
            existing = self._store.find_by_execution(kind, plan_id)
            checkpoint_id = existing["checkpoint_id"] if existing else uuid.uuid4().hex
            created_at = existing["created_at"] if existing else now
            if status in _TERMINAL_RUN:
                recovery_status = "completed" if status == "completed" else "cancelled"
                if status == "interrupted":
                    recovery_status = "interrupted"
                approval_pending = False
            elif status in _ACTIVE_RUN or status in {"waiting", "running"}:
                recovery_status = "active"
                approval_pending = bool(run.get("action_id") or run.get("pending") or run.get("awaiting"))
            else:
                recovery_status = "needs_review"
                approval_pending = False
            steps = run.get("steps") or []
            index = int(run.get("index") or 0)
            try:
                restart_payload = payload_fn(run)
            except Exception:
                restart_payload = {}
                recovery_status = "needs_review"
            summary = _summary(kind, run)
            row = {
                "checkpoint_id": checkpoint_id,
                "execution_id": plan_id,
                "kind": kind,
                "status": status or "unknown",
                "recovery_status": recovery_status,
                "step_index": index,
                "step_count": len(steps) if isinstance(steps, list) else 0,
                "approval_pending": approval_pending,
                "summary": summary,
                "restart_payload": restart_payload,
                "created_at": created_at,
                "updated_at": now,
            }
            if not _valid_row(row):
                self._audit.write(status="recovery_rejected", outcome="rejected", detail="Corrupt checkpoint refused")
                return
            self._store.upsert(row)

    def _require(self, checkpoint_id: object) -> dict | None:
        if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
            return None
        row = self._store.get(checkpoint_id)
        if row is None or not _valid_row(row):
            return None
        return row

    def _invalidate_live_approvals(self, row: dict) -> None:
        """Cancel any live execution tied to this checkpoint without replaying it."""
        kind = row["kind"]
        execution_id = row["execution_id"]
        try:
            if kind == "goal" and self._goal is not None:
                snap = self._goal.snapshot()
                if snap.get("plan_id") == execution_id and snap.get("status") in {"proposed", "awaiting_approval"}:
                    self._goal.cancel()
            elif kind == "multi_app" and self._multi_app is not None:
                snap = self._multi_app.status()
                run = snap.get("run") or {}
                if run.get("plan_id") == execution_id and run.get("status") not in _TERMINAL_RUN:
                    self._multi_app.cancel()
            elif kind == "browser_workflow" and self._bridge is not None:
                snap = self._bridge.workflow.status()
                run = snap.get("run") or {}
                if run.get("plan_id") == execution_id and run.get("status") not in _TERMINAL_RUN:
                    self._bridge.workflow.cancel()
        except Exception:
            pass

    def _restart_goal(self, payload: dict) -> dict:
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip() or looks_like_secret(task):
            raise ValueError("unsafe goal restart")
        if self._goal is None:
            raise ValueError("goal unavailable")
        return self._goal.propose(task)

    def _restart_multi_app(self, payload: dict) -> dict:
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("unsafe multi-app restart")
        if self._multi_app is None:
            raise ValueError("multi-app unavailable")
        return self._multi_app.propose({"steps": steps})

    def _restart_browser(self, payload: dict) -> dict:
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("unsafe browser restart")
        if self._bridge is None:
            raise ValueError("browser unavailable")
        return self._bridge.workflow.propose({"steps": steps})

    def _fail(self, text: str) -> dict:
        self._notice = text
        body = self.status()
        body["ok"] = False
        body["text"] = text
        return body


def _goal_payload(run: dict) -> dict:
    task = run.get("task")
    if not isinstance(task, str) or not task.strip() or looks_like_secret(task):
        return {}
    # Do not persist write contents for restart; user re-plans via goal text only.
    return {"task": task[:400]}


def _multi_app_payload(run: dict) -> dict:
    steps = run.get("steps")
    if not isinstance(steps, list):
        return {}
    out = []
    for step in steps:
        if not isinstance(step, dict):
            return {}
        capability = step.get("capability")
        if capability not in {"system_info", "open_notepad", "open_calculator", "desktop_engine", "browser_workflow"}:
            return {}
        row = {"capability": capability}
        payload = step.get("payload") if isinstance(step.get("payload"), dict) else {}
        if capability in {"desktop_engine", "browser_workflow"}:
            action = payload.get("action") or step.get("action")
            if not isinstance(action, str):
                return {}
            row["action"] = action
            for key in ("window_id", "element_id", "shortcut"):
                value = payload.get(key) or step.get(key) or ""
                if value:
                    row[key] = value
            text = payload.get("text") or step.get("text") or ""
            if text:
                if looks_like_secret(text):
                    return {}
                row["text"] = text
        out.append(row)
    return {"steps": out}


def _browser_payload(run: dict) -> dict:
    steps = run.get("steps")
    if not isinstance(steps, list):
        return {}
    out = []
    for step in steps:
        if not isinstance(step, dict):
            return {}
        action = step.get("action")
        if action not in {"observe", "click", "focus", "type", "select_all"}:
            return {}
        row = {"action": action}
        element_id = step.get("element_id") or ""
        if action != "observe":
            if not isinstance(element_id, str) or not element_id:
                # Public view may omit element_id; cannot safely restart.
                return {}
            row["element_id"] = element_id
        text = step.get("text") or ""
        if action == "type":
            if not isinstance(text, str) or not text or looks_like_secret(text):
                return {}
            row["text"] = text
        out.append(row)
    return {"steps": out}


def _summary(kind: str, run: dict) -> str:
    if kind == "goal":
        task = run.get("task") or ""
        return ("Goal: " + str(task)[:80]) if task else "Goal workflow"
    if kind == "multi_app":
        apps = run.get("apps") or []
        return "Multi-app: " + (", ".join(str(item) for item in apps)[:80] or "workflow")
    return "Browser workflow"


def _public(row: dict) -> dict | None:
    if not _valid_row(row):
        return None
    return {
        "checkpoint_id": row["checkpoint_id"],
        "execution_id": row["execution_id"],
        "kind": row["kind"],
        "status": row["status"],
        "recovery_status": row["recovery_status"],
        "step_index": row["step_index"],
        "step_count": row["step_count"],
        "approval_pending": row["approval_pending"],
        "summary": row["summary"],
        "updated_at": row["updated_at"],
        "can_restart": bool(row.get("restart_payload")),
    }


def _valid_row(row: dict) -> bool:
    if row.get("kind") not in _KINDS:
        return False
    if not isinstance(row.get("checkpoint_id"), str) or not _ID.fullmatch(row["checkpoint_id"]):
        return False
    if not isinstance(row.get("execution_id"), str) or not _ID.fullmatch(row["execution_id"]):
        return False
    if not isinstance(row.get("summary"), str) or len(row["summary"]) > 200:
        return False
    if row.get("restart_payload") is not None and not isinstance(row.get("restart_payload"), dict):
        return False
    try:
        index = int(row.get("step_index", -1))
        count = int(row.get("step_count", -1))
    except (TypeError, ValueError):
        return False
    if index < 0 or count < 0 or index > max(count, 0) + 1:
        return False
    return True


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
