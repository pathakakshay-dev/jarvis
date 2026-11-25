"""Approved browser session listing and local inspection.

This module does not automate browsers, open debugger ports, or send page data
to a provider. Titles and URLs are treated as untrusted local display data.
"""

from __future__ import annotations

import re
import threading
import uuid

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_ID = re.compile(r"^[0-9a-f]{32}$")
_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'!?:;()\[\]/_+\-]{0,119}$")
_ACTIONS = frozenset({"list", "inspect"})
_MAX_TITLE = 120


class BrowserIntelligence:
    """List allowlisted browser windows, then inspect one after approval."""

    def __init__(self, halt: HaltController, audit: AuditLog, *, browser=None, clock=None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid browser inspection lifetime")
        self._halt = halt
        self._audit = audit
        self._browser = browser if browser is not None else _browser_port()
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled_flag = False
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._sessions: dict | None = None
        self._inspection: dict | None = None
        self._notice = ""
        self._lock = threading.RLock()

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            return self._view_locked(False)

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled_flag = bool(enabled)
            return self._enabled()

    def preview(self, payload: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The action was cancelled.")
            if not self._enabled():
                self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser intelligence is not enabled")
                return self._stop_view("Browser intelligence is not enabled.")
            if self._pending is not None:
                return self._stop_view("A browser action is already waiting.")
            try:
                request = _request(payload)
                pending = self._bind_locked(request)
            except _BrowserError as exc:
                self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view(exc.text)
            action_id = uuid.uuid4().hex
            if action_id in self._used or self._halt.approval_state(action_id) is not None:
                return self._stop_view("That action is not allowed.")
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._stop_view("The action was cancelled.")
            pending.update({"action_id": action_id, "generation": generation, "expires": self._clock() + self._ttl_s})
            try:
                self._halt.request_approval(action_id)
            except HaltedError:
                self._used.add(action_id)
                return self._stop_view("The action was cancelled.")
            if not self._halt.is_current(generation) or self._halt.approval_state(action_id) != "pending":
                self._used.add(action_id)
                return self._stop_view("The action was cancelled.")
            self._pending = pending
            self._notice = pending["summary"]
            self._audit.write(status="browser_preview", outcome="pending", detail="Browser action previewed")
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view("That action is not waiting for approval.")
            pending = self._pending
            if pending is None or pending["action_id"] != action_id:
                self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser action rejected")
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._audit.write(status="browser_cancelled", outcome="cancelled", detail="Browser action cancelled")
                return self._stop_view("The action was cancelled.")
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="browser_denied", outcome="denied", detail="Browser action denied")
                self._notice = "The action was denied."
                return self._view_locked(False)
            if not self._enabled():
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser intelligence is not enabled")
                self._notice = "Browser intelligence is not enabled."
                return self._view_locked(False)
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                self._audit.write(status="browser_cancelled", outcome="cancelled", detail="Browser action cancelled")
                return self._stop_view("The action was cancelled.")
            generation = pending["generation"]
            deadline = pending["expires"]
            self._consume_locked(action_id)
            if not self._allowed(generation, deadline):
                return self._blocked_locked(generation, deadline)
            try:
                performed = self._perform_locked(pending, generation, deadline)
            except Exception:
                performed = False
            if performed and pending["action"] == "list":
                outcome = "completed" if self._sessions is not None else "rejected"
                self._audit.write(status="browser_" + outcome, outcome=outcome, detail="Browser list finished")
                return self._view_locked(self._sessions is not None)
            if performed and pending["action"] == "inspect":
                outcome = "completed" if self._inspection is not None else "rejected"
                self._audit.write(status="browser_" + outcome, outcome=outcome, detail="Browser inspection finished")
                return self._view_locked(self._inspection is not None)
            return self._blocked_locked(generation, deadline)

    def cancel(self) -> dict:
        with self._lock:
            self._expire_locked()
            pending = self._pending
            if pending is not None:
                action_id = pending["action_id"]
                if self._halt.approval_state(action_id) == "pending":
                    self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
            self._notice = "The action was cancelled."
            self._audit.write(status="browser_cancelled", outcome="cancelled", detail="Browser action cancelled")
            return self._view_locked(False)

    def clear(self) -> dict:
        with self._lock:
            self._expire_locked()
            self._sessions = None
            self._inspection = None
            self._notice = "The browser inspection was cleared."
            self._audit.write(status="browser_cleared", outcome="completed", detail="Browser inspection cleared")
            return self._view_locked(False)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._pending = None
                self._sessions = None
                self._inspection = None
                self._notice = ""

    def _bind_locked(self, request: dict) -> dict:
        action = request["action"]
        if action == "list":
            if request["session_id"]:
                raise _BrowserError("That browser action is not valid.")
            return {"action": action, "summary": "List open Edge and Chrome windows.", "session_id": "", "handle": 0, "pid": 0, "image": "", "class_name": "", "title": ""}
        sessions = self._sessions
        if sessions is None or not self._halt.is_current(sessions["generation"]) or self._clock() >= sessions["expires"]:
            raise _BrowserError("Find the browser windows before inspecting.")
        session = sessions["sessions"].get(request["session_id"])
        if session is None:
            raise _BrowserError("That browser session is not available.")
        return {
            "action": action,
            "summary": "Inspect " + session["browser"] + ".",
            "session_id": session["session_id"],
            "handle": session["handle"],
            "pid": session["pid"],
            "image": session["image"],
            "class_name": session["class_name"],
            "title": session["title"],
        }

    def _perform_locked(self, pending: dict, generation: int, deadline: float) -> bool:
        allow = lambda: self._allowed(generation, deadline)
        if not allow():
            return False
        if pending["action"] == "list":
            scene = self._browser.list_sessions()
            if not allow():
                self._sessions = None
                return False
            self._sessions = self._accept_sessions(scene, generation)
            return True
        if pending["action"] != "inspect":
            return False
        if not allow():
            return False
        inspected = self._browser.inspect_session(
            pending["handle"],
            pending["pid"],
            pending["image"],
            pending["class_name"],
            pending["title"],
        )
        if not allow():
            self._inspection = None
            return False
        if inspected is None:
            self._inspection = None
            return False
        self._inspection = self._accept_inspection(inspected, generation)
        return self._inspection is not None

    def _accept_sessions(self, scene: object, generation: int) -> dict | None:
        rows = scene.get("sessions") if isinstance(scene, dict) else None
        if not isinstance(rows, list):
            self._notice = "No browser window was found."
            return None
        sessions: dict[str, dict] = {}
        for row in rows:
            built = _accept_session(row)
            if built is None:
                continue
            session_id = uuid.uuid4().hex
            built["session_id"] = session_id
            sessions[session_id] = built
        if not sessions:
            self._notice = "No browser window was found."
            return None
        self._notice = "Choose a browser window."
        return {"generation": generation, "expires": self._clock() + self._ttl_s, "sessions": sessions}

    def _accept_inspection(self, row: object, generation: int) -> dict | None:
        if not isinstance(row, dict):
            self._notice = "That browser session could not be inspected."
            return None
        browser = row.get("browser")
        tab_title = row.get("tab_title")
        url = row.get("url", "")
        tab_count = row.get("tab_count")
        url_available = row.get("url_available")
        if browser not in {"Google Chrome", "Microsoft Edge"}:
            self._notice = "That browser session could not be inspected."
            return None
        if not isinstance(tab_title, str) or not _TITLE.fullmatch(tab_title) or looks_like_secret(tab_title):
            self._notice = "That browser title is not allowed."
            return None
        if url_available is not False or url not in ("", None):
            # This foundation never accepts a URL from the safe local path.
            self._notice = "That browser session could not be inspected."
            return None
        if not isinstance(tab_count, int) or isinstance(tab_count, bool) or tab_count < 1 or tab_count > 100:
            self._notice = "That browser session could not be inspected."
            return None
        if looks_like_secret(str(url)):
            self._notice = "That browser session could not be inspected."
            return None
        self._notice = "The browser session was inspected."
        return {
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
            "browser": browser,
            "tab_title": tab_title,
            "url": "",
            "url_available": False,
            "tab_count": tab_count,
            "note": "URL is not available from a safe local inspection.",
        }

    def _allowed(self, generation: int, deadline: float) -> bool:
        return self._halt.is_current(generation) and self._enabled() and self._clock() < deadline

    def _blocked_locked(self, generation: int, deadline: float) -> dict:
        if not self._halt.is_current(generation):
            self._notice = "The action was cancelled."
            self._audit.write(status="browser_cancelled", outcome="cancelled", detail="Browser action cancelled")
        elif not self._enabled():
            self._notice = "Browser intelligence is not enabled."
            self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser intelligence is not enabled")
        elif self._clock() >= deadline:
            self._notice = "That approval has expired."
            self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser action expired")
        else:
            self._notice = "The browser changed before it could be inspected."
            self._audit.write(status="browser_rejected", outcome="rejected", detail="Browser target changed")
        return self._view_locked(False)

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
        sessions = self._sessions
        if sessions is not None and (not self._halt.is_current(sessions["generation"]) or self._clock() >= sessions["expires"]):
            self._sessions = None
        inspection = self._inspection
        if inspection is not None and (not self._halt.is_current(inspection["generation"]) or self._clock() >= inspection["expires"]):
            self._inspection = None
        return expired

    def _enabled(self) -> bool:
        return self._enabled_flag

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
                "state": "waiting for approval",
            }
        return {
            "ok": self._pending is not None or performed or self._notice == "The browser session was inspected.",
            "permission_enabled": self._enabled(),
            "text": self._notice,
            "pending": pending,
            "sessions": _public_sessions(self._sessions),
            "inspection": _public_inspection(self._inspection),
            "performed": performed,
            "executed": False,
        }


def _request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _BrowserError("That browser action is not valid.")
    if set(payload) - {"action", "session_id"}:
        raise _BrowserError("That browser action is not valid.")
    action = payload.get("action")
    session_id = payload.get("session_id", "")
    if action not in _ACTIONS:
        raise _BrowserError("That browser action is not allowed.")
    if not isinstance(session_id, str):
        raise _BrowserError("That browser action is not valid.")
    if session_id and not _ID.fullmatch(session_id):
        raise _BrowserError("That browser session is not available.")
    if looks_like_secret(session_id):
        raise _BrowserError("That browser action is not supported.")
    return {"action": action, "session_id": session_id}


def _accept_session(row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    image = row.get("image")
    class_name = row.get("class_name")
    title = row.get("title")
    browser = row.get("browser")
    if image not in {"chrome.exe", "msedge.exe"} or class_name != "Chrome_WidgetWin_1":
        return None
    if browser not in {"Google Chrome", "Microsoft Edge"}:
        return None
    if row.get("protected") or _protected(str(title), str(image), str(class_name)):
        return None
    if not isinstance(title, str) or not title or len(title) > _MAX_TITLE or looks_like_secret(title):
        return None
    handle = row.get("handle")
    pid = row.get("pid")
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return {
        "handle": handle,
        "pid": pid,
        "image": image,
        "class_name": class_name,
        "title": title,
        "browser": browser,
    }


def _public_sessions(sessions: dict | None) -> dict | None:
    if sessions is None:
        return None
    rows = []
    for session in sessions["sessions"].values():
        rows.append(
            {
                "session_id": session["session_id"],
                "browser": session["browser"],
                "title": session["title"],
            }
        )
    return {"sessions": rows}


def _public_inspection(inspection: dict | None) -> dict | None:
    if inspection is None:
        return None
    return {
        "browser": inspection["browser"],
        "tab_title": inspection["tab_title"],
        "url": "",
        "url_available": False,
        "tab_count": inspection["tab_count"],
        "note": inspection["note"],
    }


def _protected(title: str, image: str, class_name: str) -> bool:
    folded = " ".join((title, image, class_name)).lower()
    return any(
        marker in folded
        for marker in ("password", "credential", "user account control", "windows security", "bitlocker", "consent.exe", "sign in", "login")
    )


def _browser_port():
    from jarvis.browser.windows import inspection_from_session, list_browser_sessions, read_browser_session

    class _Port:
        def list_sessions(self) -> dict:
            return list_browser_sessions()

        def inspect_session(self, handle: int, pid: int, image: str, class_name: str, title: str) -> dict | None:
            return read_browser_session(handle, pid, image, class_name, title)

    return _Port()


def _monotonic() -> float:
    import time

    return time.monotonic()


class _BrowserError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
