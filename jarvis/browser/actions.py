"""Approval-gated browser actions over the existing Native Messaging bridge.

Supports click, focus, type, and select-all on short-lived element identities.
Execution is claim-based: the extension pulls one approved job, revalidates the
DOM, then reports the result. No Goal Agent, desktop engine, or planner coupling.
"""

from __future__ import annotations

import re
import threading
import uuid
from urllib.parse import urlparse

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_ID = re.compile(r"^[0-9a-f]{32}$")
_ELEMENT_ID = re.compile(r"^[0-9a-f]{16}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{32}$")
_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'!?:;()\[\]/_+\-]{0,119}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'!?:;()\[\]/_+\-]{0,79}$")
_TEXT = re.compile(r"^[A-Za-z0-9 .,!?'\-]{1,200}$")
_TAG = frozenset({"button", "input", "textarea"})
_KINDS = frozenset({"button", "text_field"})
_INPUT_TYPES = frozenset({"", "text", "search", "email", "tel", "url", "button"})
_ACTIONS = frozenset({"click", "focus", "type", "select_all"})
_MAX_TARGETS = 20
_MAX_URL = 500
_RATE_LIMIT = 10
_RATE_WINDOW_S = 60.0
_SENSITIVE_PAGE = re.compile(
    r"(sign[\s_-]?in|log[\s_-]?in|password|passwd|auth|oauth|checkout|payment|billing|credit[\s_-]?card|bank)",
    re.I,
)
_SENSITIVE_TYPES = frozenset(
    {
        "password",
        "hidden",
        "file",
        "submit",
        "reset",
        "image",
        "checkbox",
        "radio",
        "range",
        "color",
        "date",
        "datetime-local",
        "month",
        "week",
        "time",
        "number",
    }
)


class BrowserActions:
    """One approved browser action at a time, claimed by the extension for execution."""

    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        bridge,
        lock: threading.RLock,
        clock=None,
        ttl_s: float = _TTL_S,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid browser action lifetime")
        self._halt = halt
        self._audit = audit
        self._bridge = bridge
        self._lock = lock
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled_flag = False
        self._targets: dict | None = None
        self._pending: dict | None = None
        self._ready: dict | None = None
        self._claimed: dict | None = None
        self._used: set[str] = set()
        self._last_result: dict | None = None
        self._notice = ""
        self._times: list[float] = []

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            return self._view_locked(False)

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled_flag = bool(enabled)
            if not self._enabled_flag:
                self._clear_transient_locked("Browser actions are off.")
            else:
                self._notice = "Browser actions are on. Scan targets from the extension, then propose one action."
            return self._enabled_flag

    def busy(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._pending is not None or self._ready is not None or self._claimed is not None

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._cancel_pending_locked()
                self._targets = None
                self._ready = None
                self._claimed = None
                self._last_result = None
                self._notice = ""

    def drop_state(self) -> None:
        with self._lock:
            self._cancel_pending_locked()
            self._targets = None
            self._ready = None
            self._claimed = None
            self._last_result = None

    def clear(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._cancel_pending_locked()
            self._targets = None
            self._ready = None
            self._claimed = None
            self._last_result = None
            self._notice = "Browser action state was cleared."
            self._audit.write(status="browser_action_cleared", outcome="completed", detail="Browser action state cleared")
            return self._view_locked(False)

    def cancel(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._cancel_pending_locked()
            self._ready = None
            self._claimed = None
            self._notice = "The action was cancelled."
            self._audit.write(status="browser_action_cancelled", outcome="cancelled", detail="Browser action cancelled")
            return self._view_locked(False)

    def propose(self, payload: object, *, for_workflow: bool = False) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The action was cancelled.")
            if not self._enabled_flag:
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser actions are not enabled")
                return self._stop_view("Browser actions are not enabled.")
            if self._bridge.has_bridge_pending():
                return self._stop_view("The browser bridge is busy.")
            workflow = getattr(self._bridge, "workflow", None)
            if not for_workflow and workflow is not None and workflow.busy():
                return self._stop_view("A browser workflow is already running.")
            if self._pending is not None or self._ready is not None or self._claimed is not None:
                return self._stop_view("A browser action is already waiting.")
            session = self._bridge.session_snapshot()
            if session is None:
                return self._stop_view("Connect the browser bridge first.")
            targets = self._targets
            if targets is None or not self._halt.is_current(targets["generation"]) or self._clock() >= targets["expires"]:
                self._targets = None
                return self._stop_view("Scan page targets before proposing an action.")
            if targets["session_id"] != session["session_id"]:
                self._targets = None
                return self._stop_view("Scan page targets again for the current session.")
            try:
                request = _proposal(payload)
            except _ActionError as exc:
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view(exc.text)
            element = targets["elements"].get(request["element_id"])
            if element is None:
                return self._stop_view("That page target is not available.")
            try:
                _compatible(request["action"], element)
                _page_allowed(targets["tab_title"], targets["tab_url"])
            except _ActionError as exc:
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view(exc.text)
            if request["action"] == "type" and looks_like_secret(request["text"]):
                return self._stop_view("That text is not allowed.")
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._stop_view("The action was cancelled.")
            action_id = uuid.uuid4().hex
            if action_id in self._used or self._halt.approval_state(action_id) is not None:
                return self._stop_view("That action is not waiting for approval.")
            try:
                self._halt.request_approval(action_id)
            except HaltedError:
                self._used.add(action_id)
                return self._stop_view("The action was cancelled.")
            if not self._halt.is_current(generation) or self._halt.approval_state(action_id) != "pending":
                self._used.add(action_id)
                return self._stop_view("The action was cancelled.")
            summary = _summary(request["action"], element, request["text"])
            self._pending = {
                "action_id": action_id,
                "action": request["action"],
                "generation": generation,
                "expires": self._clock() + self._ttl_s,
                "session_id": session["session_id"],
                "browser": session["browser"],
                "tab_title": targets["tab_title"],
                "tab_url": targets["tab_url"],
                "element_id": element["element_id"],
                "fingerprint": element["fingerprint"],
                "kind": element["kind"],
                "tag": element["tag"],
                "input_type": element["input_type"],
                "label": element["label"],
                "text": request["text"],
                "summary": summary,
            }
            self._last_result = None
            self._notice = summary
            self._audit.write(status="browser_action_preview", outcome="pending", detail="Browser action previewed")
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view("That action is not waiting for approval.")
            pending = self._pending
            if pending is None or pending["action_id"] != action_id:
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._audit.write(status="browser_action_cancelled", outcome="cancelled", detail="Browser action cancelled")
                return self._stop_view("The action was cancelled.")
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="browser_action_denied", outcome="denied", detail="Browser action denied")
                self._notice = "The action was denied."
                return self._view_locked(False)
            if not self._enabled_flag:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser actions are not enabled")
                return self._stop_view("Browser actions are not enabled.")
            session = self._bridge.session_snapshot()
            if session is None or session["session_id"] != pending["session_id"]:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                return self._stop_view("Browser bridge is not connected.")
            targets = self._targets
            if (
                targets is None
                or targets["session_id"] != pending["session_id"]
                or pending["element_id"] not in targets["elements"]
                or targets["elements"][pending["element_id"]]["fingerprint"] != pending["fingerprint"]
                or targets["tab_url"] != pending["tab_url"]
                or targets["tab_title"] != pending["tab_title"]
            ):
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action target mismatch")
                return self._stop_view("The page target no longer matches.")
            try:
                _page_allowed(pending["tab_title"], pending["tab_url"])
                _compatible(pending["action"], targets["elements"][pending["element_id"]])
            except _ActionError as exc:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                return self._stop_view(exc.text)
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                return self._stop_view("The action was cancelled.")
            if self._clock() >= pending["expires"] or not self._halt.is_current(pending["generation"]):
                self._consume_locked(action_id)
                return self._stop_view("That approval has expired." if self._halt.is_current(pending["generation"]) else "The action was cancelled.")
            self._ready = {
                "action_id": action_id,
                "action": pending["action"],
                "generation": pending["generation"],
                "expires": self._clock() + self._ttl_s,
                "session_id": pending["session_id"],
                "browser": pending["browser"],
                "tab_title": pending["tab_title"],
                "tab_url": pending["tab_url"],
                "element_id": pending["element_id"],
                "fingerprint": pending["fingerprint"],
                "kind": pending["kind"],
                "tag": pending["tag"],
                "input_type": pending["input_type"],
                "label": pending["label"],
                "text": pending["text"],
                "summary": pending["summary"],
            }
            self._pending = None
            self._used.add(action_id)
            self._notice = "Approved. Run the action from the extension popup."
            self._audit.write(status="browser_action_approved", outcome="pending", detail="Browser action approved for claim")
            return self._view_locked(True)

    def handle_ingress(self, message: dict, *, session: dict | None, bridge_busy: bool) -> dict:
        with self._lock:
            self._expire_locked()
            if not self._rate_ok_locked():
                self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action rate limited")
                return {"ok": False, "error": "rate_limited"}
            if not self._enabled_flag:
                return {"ok": False, "error": "actions_disabled"}
            if session is None:
                return {"ok": False, "error": "session"}
            kind = message["type"]
            if kind == "action_targets":
                return self._targets_locked(message, session, bridge_busy)
            if kind == "action_claim":
                return self._claim_locked(message, session)
            if kind == "action_result":
                return self._result_locked(message, session)
            return {"ok": False, "error": "unsupported"}

    def _targets_locked(self, message: dict, session: dict, bridge_busy: bool) -> dict:
        if bridge_busy or self._pending is not None or self._ready is not None or self._claimed is not None:
            return {"ok": False, "error": "busy"}
        if message["session_id"] != session["session_id"] or message["browser"] != session["browser"]:
            return {"ok": False, "error": "session"}
        title = message["tab_title"]
        url = message["tab_url"]
        if looks_like_secret(title) or looks_like_secret(url):
            return {"ok": False, "error": "rejected"}
        if not _TITLE.fullmatch(title) or not _safe_url(url):
            return {"ok": False, "error": "rejected"}
        try:
            _page_allowed(title, url)
            elements = _elements(message["elements"])
        except _ActionError:
            self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action targets rejected")
            return {"ok": False, "error": "targets"}
        generation = self._halt.capture()
        if not self._halt.is_current(generation):
            return {"ok": False, "error": "cancelled"}
        self._targets = {
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
            "session_id": session["session_id"],
            "browser": session["browser"],
            "tab_title": title,
            "tab_url": url,
            "elements": elements,
        }
        self._notice = f"Scanned {len(elements)} page target(s)."
        self._audit.write(status="browser_action_targets", outcome="completed", detail="Browser action targets scanned")
        return {"ok": True, "type": "targets_ok", "count": len(elements)}

    def _claim_locked(self, message: dict, session: dict) -> dict:
        if message["session_id"] != session["session_id"] or message["browser"] != session["browser"]:
            return {"ok": False, "error": "session"}
        if not self._enabled_flag:
            return {"ok": False, "error": "actions_disabled"}
        ready = self._ready
        if ready is None:
            return {"ok": False, "error": "none"}
        if ready["session_id"] != session["session_id"]:
            self._ready = None
            return {"ok": False, "error": "session"}
        if not self._halt.is_current(ready["generation"]) or self._clock() >= ready["expires"]:
            self._ready = None
            return {"ok": False, "error": "expired"}
        if self._claimed is not None:
            return {"ok": False, "error": "busy"}
        self._claimed = {
            **ready,
            "expires": self._clock() + min(30.0, self._ttl_s),
        }
        self._ready = None
        job = {
            "ok": True,
            "type": "action_job",
            "action_id": self._claimed["action_id"],
            "action": self._claimed["action"],
            "element_id": self._claimed["element_id"],
            "fingerprint": self._claimed["fingerprint"],
            "tab_title": self._claimed["tab_title"],
            "tab_url": self._claimed["tab_url"],
            "kind": self._claimed["kind"],
            "tag": self._claimed["tag"],
            "input_type": self._claimed["input_type"],
        }
        if self._claimed["action"] == "type":
            job["text"] = self._claimed["text"]
        self._notice = "Action claimed by the extension."
        self._audit.write(status="browser_action_claimed", outcome="pending", detail="Browser action claimed")
        return job

    def _result_locked(self, message: dict, session: dict) -> dict:
        claimed = self._claimed
        if claimed is None:
            return {"ok": False, "error": "none"}
        if message["session_id"] != session["session_id"] or message["browser"] != session["browser"]:
            return {"ok": False, "error": "session"}
        if message["action_id"] != claimed["action_id"]:
            self._audit.write(status="browser_action_rejected", outcome="rejected", detail="Browser action result rejected")
            return {"ok": False, "error": "forged"}
        if not self._enabled_flag:
            self._claimed = None
            return {"ok": False, "error": "actions_disabled"}
        if not self._halt.is_current(claimed["generation"]) or self._clock() >= claimed["expires"]:
            self._claimed = None
            return {"ok": False, "error": "expired"}
        if message["tab_url"] != claimed["tab_url"] or message["tab_title"] != claimed["tab_title"]:
            self._last_result = {"action_id": claimed["action_id"], "outcome": "mismatch", "verified": False}
            self._claimed = None
            self._notice = "The tab changed before the action finished."
            self._audit.write(status="browser_action_failed", outcome="failed", detail="Browser action tab mismatch")
            self._notify_workflow_locked(self._last_result)
            return {"ok": False, "error": "tab"}
        if message["element_id"] != claimed["element_id"] or message["fingerprint"] != claimed["fingerprint"]:
            self._last_result = {"action_id": claimed["action_id"], "outcome": "mismatch", "verified": False}
            self._claimed = None
            self._notice = "The page target no longer matches."
            self._audit.write(status="browser_action_failed", outcome="failed", detail="Browser action element mismatch")
            self._notify_workflow_locked(self._last_result)
            return {"ok": False, "error": "element"}
        if message["outcome"] == "completed" and message["verified"] is True:
            self._last_result = {
                "action_id": claimed["action_id"],
                "action": claimed["action"],
                "outcome": "completed",
                "verified": True,
                "label": claimed["label"],
            }
            self._claimed = None
            self._notice = "The browser action finished."
            self._audit.write(status="browser_action_completed", outcome="completed", detail="Browser action completed")
            self._notify_workflow_locked(self._last_result)
            return {"ok": True, "type": "action_done"}
        self._last_result = {
            "action_id": claimed["action_id"],
            "action": claimed["action"],
            "outcome": message["outcome"],
            "verified": False,
            "label": claimed["label"],
        }
        self._claimed = None
        self._notice = "The browser action failed verification."
        self._audit.write(status="browser_action_failed", outcome="failed", detail="Browser action verification failed")
        self._notify_workflow_locked(self._last_result)
        return {"ok": False, "error": "verify"}

    def _clear_transient_locked(self, notice: str) -> None:
        self._cancel_pending_locked()
        self._targets = None
        self._ready = None
        self._claimed = None
        self._last_result = None
        self._notice = notice

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
            self._notice = "That approval has expired." if self._halt.is_current(pending["generation"]) else "The action was cancelled."
            expired = action_id
        if self._targets is not None and (not self._halt.is_current(self._targets["generation"]) or self._clock() >= self._targets["expires"]):
            self._targets = None
        if self._ready is not None and (not self._halt.is_current(self._ready["generation"]) or self._clock() >= self._ready["expires"]):
            self._ready = None
            self._notice = "The approved action expired before it was run."
            self._notify_workflow_timeout_locked()
        if self._claimed is not None and (not self._halt.is_current(self._claimed["generation"]) or self._clock() >= self._claimed["expires"]):
            self._claimed = None
            self._notice = "The claimed action expired."
            self._notify_workflow_timeout_locked()
        return expired

    def _notify_workflow_locked(self, result: dict) -> None:
        workflow = getattr(self._bridge, "workflow", None)
        if workflow is not None:
            workflow.on_action_result_locked(result)

    def _notify_workflow_timeout_locked(self) -> None:
        workflow = getattr(self._bridge, "workflow", None)
        if workflow is not None:
            workflow.on_action_timeout_locked()

    def _rate_ok_locked(self) -> bool:
        now = self._clock()
        self._times = [stamp for stamp in self._times if now - stamp < _RATE_WINDOW_S]
        if len(self._times) >= _RATE_LIMIT:
            return False
        self._times.append(now)
        return True

    def _stop_view(self, text: str) -> dict:
        self._notice = text
        return self._view_locked(False)

    def _view_locked(self, performed: bool) -> dict:
        pending = None
        if self._pending is not None:
            pending = {
                "action_id": self._pending["action_id"],
                "action": self._pending["action"],
                "summary": self._pending["summary"],
                "browser": self._pending["browser"],
                "tab_title": self._pending["tab_title"],
                "tab_url": self._pending["tab_url"],
                "element_id": self._pending["element_id"],
                "kind": self._pending["kind"],
                "tag": self._pending["tag"],
                "label": self._pending["label"],
                "text": self._pending["text"] if self._pending["action"] == "type" else "",
                "state": "waiting for approval",
            }
        ready = None
        if self._ready is not None:
            ready = {
                "action_id": self._ready["action_id"],
                "action": self._ready["action"],
                "summary": self._ready["summary"],
                "element_id": self._ready["element_id"],
                "label": self._ready["label"],
                "state": "approved — run from the extension",
            }
        targets = None
        if self._targets is not None:
            targets = {
                "tab_title": self._targets["tab_title"],
                "tab_url": self._targets["tab_url"],
                "elements": [
                    {
                        "element_id": row["element_id"],
                        "kind": row["kind"],
                        "tag": row["tag"],
                        "input_type": row["input_type"],
                        "label": row["label"],
                    }
                    for row in self._targets["elements"].values()
                ],
            }
        return {
            "ok": self._pending is not None or performed or bool(self._last_result and self._last_result.get("outcome") == "completed"),
            "permission_enabled": self._enabled_flag,
            "text": self._notice,
            "pending": pending,
            "ready": ready,
            "claimed": self._claimed is not None,
            "targets": targets,
            "result": self._last_result,
            "performed": performed,
            "executed": bool(self._last_result and self._last_result.get("outcome") == "completed"),
        }


def parse_action_message(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _ActionError("invalid")
    kind = payload.get("type")
    if kind == "action_targets":
        allowed = {"type", "session_id", "browser", "tab_title", "tab_url", "elements"}
        if set(payload) - allowed:
            raise _ActionError("invalid")
        session_id = payload.get("session_id")
        browser = payload.get("browser")
        tab_title = payload.get("tab_title")
        tab_url = payload.get("tab_url")
        elements = payload.get("elements")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise _ActionError("session")
        if browser not in {"Google Chrome", "Microsoft Edge"}:
            raise _ActionError("browser")
        if not isinstance(tab_title, str) or not tab_title or len(tab_title) > 120:
            raise _ActionError("title")
        if not isinstance(tab_url, str) or not tab_url or len(tab_url) > _MAX_URL:
            raise _ActionError("url")
        if not isinstance(elements, list) or len(elements) > _MAX_TARGETS:
            raise _ActionError("targets")
        return {
            "type": kind,
            "session_id": session_id,
            "browser": browser,
            "tab_title": tab_title,
            "tab_url": tab_url,
            "elements": elements,
        }
    if kind == "action_claim":
        allowed = {"type", "session_id", "browser"}
        if set(payload) - allowed:
            raise _ActionError("invalid")
        session_id = payload.get("session_id")
        browser = payload.get("browser")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise _ActionError("session")
        if browser not in {"Google Chrome", "Microsoft Edge"}:
            raise _ActionError("browser")
        return {"type": kind, "session_id": session_id, "browser": browser}
    if kind == "action_result":
        allowed = {
            "type",
            "session_id",
            "browser",
            "action_id",
            "element_id",
            "fingerprint",
            "tab_title",
            "tab_url",
            "outcome",
            "verified",
        }
        if set(payload) - allowed:
            raise _ActionError("invalid")
        session_id = payload.get("session_id")
        browser = payload.get("browser")
        action_id = payload.get("action_id")
        element_id = payload.get("element_id")
        fingerprint = payload.get("fingerprint")
        tab_title = payload.get("tab_title")
        tab_url = payload.get("tab_url")
        outcome = payload.get("outcome")
        verified = payload.get("verified")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise _ActionError("session")
        if browser not in {"Google Chrome", "Microsoft Edge"}:
            raise _ActionError("browser")
        if not isinstance(action_id, str) or not _ID.fullmatch(action_id):
            raise _ActionError("forged")
        if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
            raise _ActionError("element")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise _ActionError("element")
        if not isinstance(tab_title, str) or not tab_title or len(tab_title) > 120:
            raise _ActionError("title")
        if not isinstance(tab_url, str) or not tab_url or len(tab_url) > _MAX_URL:
            raise _ActionError("url")
        if outcome not in {"completed", "failed", "mismatch", "hidden", "sensitive"}:
            raise _ActionError("invalid")
        if type(verified) is not bool:
            raise _ActionError("invalid")
        return {
            "type": kind,
            "session_id": session_id,
            "browser": browser,
            "action_id": action_id,
            "element_id": element_id,
            "fingerprint": fingerprint,
            "tab_title": tab_title,
            "tab_url": tab_url,
            "outcome": outcome,
            "verified": verified,
        }
    raise _ActionError("unsupported")


def _proposal(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _ActionError("That browser action is not valid.")
    if set(payload) - {"action", "element_id", "text"}:
        raise _ActionError("That browser action is not valid.")
    action = payload.get("action")
    element_id = payload.get("element_id")
    text = payload.get("text", "")
    if action not in _ACTIONS:
        raise _ActionError("That browser action is not allowed.")
    if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
        raise _ActionError("That page target is not available.")
    if action == "type":
        if not isinstance(text, str) or not _TEXT.fullmatch(text):
            raise _ActionError("That text is not allowed.")
    else:
        if text not in {"", None}:
            raise _ActionError("That browser action is not valid.")
        text = ""
    return {"action": action, "element_id": element_id, "text": text}


def _elements(rows: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise _ActionError("targets")
        if set(row) - {"element_id", "kind", "tag", "input_type", "label", "fingerprint"}:
            raise _ActionError("targets")
        element_id = row.get("element_id")
        kind = row.get("kind")
        tag = row.get("tag")
        input_type = row.get("input_type", "")
        label = row.get("label")
        fingerprint = row.get("fingerprint")
        if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
            raise _ActionError("targets")
        if element_id in out:
            raise _ActionError("targets")
        if kind not in _KINDS or tag not in _TAG:
            raise _ActionError("targets")
        if not isinstance(input_type, str) or input_type.lower() in _SENSITIVE_TYPES:
            raise _ActionError("targets")
        input_type = input_type.lower()
        if input_type not in _INPUT_TYPES:
            raise _ActionError("targets")
        if kind == "button" and tag == "input" and input_type not in {"button", ""}:
            raise _ActionError("targets")
        if kind == "text_field" and tag not in {"input", "textarea"}:
            raise _ActionError("targets")
        if kind == "text_field" and tag == "input" and input_type in {"", "button"}:
            if input_type == "button":
                raise _ActionError("targets")
            input_type = "text"
        if not isinstance(label, str) or not label or not _LABEL.fullmatch(label):
            raise _ActionError("targets")
        if looks_like_secret(label):
            raise _ActionError("targets")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise _ActionError("targets")
        if _SENSITIVE_PAGE.search(label):
            raise _ActionError("targets")
        out[element_id] = {
            "element_id": element_id,
            "kind": kind,
            "tag": tag,
            "input_type": input_type,
            "label": label,
            "fingerprint": fingerprint,
        }
    return out


def _compatible(action: str, element: dict) -> None:
    if action == "click":
        if element["kind"] != "button":
            raise _ActionError("That target cannot be clicked.")
        return
    if action in {"focus", "type", "select_all"}:
        if element["kind"] != "text_field":
            raise _ActionError("That target is not a text field.")
        return
    raise _ActionError("That browser action is not allowed.")


def _page_allowed(title: str, url: str) -> None:
    if _SENSITIVE_PAGE.search(title) or _SENSITIVE_PAGE.search(url):
        raise _ActionError("That page is not allowed for browser actions.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1"} or host.endswith(".localhost"):
        raise _ActionError("That page is not allowed for browser actions.")


def _summary(action: str, element: dict, text: str) -> str:
    label = element["label"]
    if action == "click":
        return f"Click {label}."
    if action == "focus":
        return f"Focus {label}."
    if action == "select_all":
        return f"Select all text in {label}."
    return f"Type into {label}."


def _safe_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL or "\x00" in value:
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if not parsed.netloc or " " in value:
        return False
    return True


def _monotonic() -> float:
    import time

    return time.monotonic()


class _ActionError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
        self.code = text if text in {"invalid", "session", "browser", "targets", "unsupported", "forged", "element", "title", "url"} else "rejected"
