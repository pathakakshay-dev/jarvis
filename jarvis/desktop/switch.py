"""Approved focus for one already-open Notepad window.

This module does not type, click, launch programs, or use the planner,
simulator, goal agent, or desktop-control actions.
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
_TITLE = re.compile(r"^\*?[A-Za-z0-9][A-Za-z0-9 .,'-]{0,119}$")
_ACTIONS = frozenset({"observe", "focus_window"})
_ALLOWED_IMAGE = "notepad.exe"
_ALLOWED_CLASS = "Notepad"
_ALLOWED_NAME = "Notepad"


class WindowSwitcher:
    """Preview one Notepad focus, then perform it only after its own approval."""

    def __init__(self, halt: HaltController, audit: AuditLog, *, desktop=None, clock=None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid window switch lifetime")
        self._halt = halt
        self._audit = audit
        self._desktop = desktop if desktop is not None else _focus_port()
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled_flag = False
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._observation: dict | None = None
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
                self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window switching is not enabled")
                return self._stop_view("Window switching is not enabled.")
            if self._pending is not None:
                return self._stop_view("A window action is already waiting.")
            try:
                request = _request(payload)
                pending = self._bind_locked(request)
            except _SwitchError as exc:
                self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window action rejected")
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
            self._audit.write(status="window_switch_preview", outcome="pending", detail="Window action previewed")
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window action rejected")
                return self._stop_view("That action is not waiting for approval.")
            pending = self._pending
            if pending is None or pending["action_id"] != action_id:
                self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window action rejected")
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._audit.write(status="window_switch_cancelled", outcome="cancelled", detail="Window action cancelled")
                return self._stop_view("The action was cancelled.")
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="window_switch_denied", outcome="denied", detail="Window action denied")
                self._notice = "The action was denied."
                return self._view_locked(False)
            if not self._enabled():
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window switching is not enabled")
                self._notice = "Window switching is not enabled."
                return self._view_locked(False)
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                self._audit.write(status="window_switch_cancelled", outcome="cancelled", detail="Window action cancelled")
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
            if performed and pending["action"] == "observe":
                if not self._notice:
                    self._notice = "No Notepad window was found."
                outcome = "completed" if self._observation is not None else "rejected"
                self._audit.write(status="window_switch_" + outcome, outcome=outcome, detail="Window list finished")
                return self._view_locked(self._observation is not None)
            if performed:
                self._audit.write(status="window_switch_completed", outcome="completed", detail="Window focus finished")
                self._notice = "Notepad was focused."
                return self._view_locked(True)
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
            self._audit.write(status="window_switch_cancelled", outcome="cancelled", detail="Window action cancelled")
            return self._view_locked(False)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._pending = None
                self._observation = None
                self._notice = ""

    def _perform_locked(self, pending: dict, generation: int, deadline: float) -> bool:
        allow = lambda: self._allowed(generation, deadline)
        if pending["action"] == "observe":
            if not allow():
                return False
            scene = self._desktop.observe()
            if not allow():
                self._observation = None
                return False
            self._observation = self._accept_scene(scene, generation)
            return True
        if pending["action"] != "focus_window":
            return False
        fresh = self._desktop.observe()
        if not allow() or not self._same_window(pending, fresh):
            return False
        return bool(self._desktop.focus(pending["handle"], allow))

    def _bind_locked(self, request: dict) -> dict:
        action = request["action"]
        if action == "observe":
            if request["window_id"]:
                raise _SwitchError("That window action is not valid.")
            return _pending(action, "List open Notepad windows.", 0, "", "", 0)
        observation = self._observation
        if observation is None or not self._halt.is_current(observation["generation"]) or self._clock() >= observation["expires"]:
            raise _SwitchError("Find the Notepad windows before focusing.")
        window = observation["windows"].get(request["window_id"])
        if window is None:
            raise _SwitchError("That window is not available.")
        return _pending(action, "Focus " + window["title"] + ".", window["handle"], window["window_id"], window["title"], window["pid"])

    def _accept_scene(self, scene: object, generation: int) -> dict | None:
        rows = scene.get("windows") if isinstance(scene, dict) else None
        if not isinstance(rows, list):
            self._notice = "No Notepad window was found."
            return None
        windows: dict[str, dict] = {}
        for row in rows:
            built = _accept_window(row)
            if built is None:
                continue
            window_id = uuid.uuid4().hex
            built["window_id"] = window_id
            windows[window_id] = built
        if not windows:
            self._notice = "No Notepad window was found."
            return None
        self._notice = "Choose a Notepad window."
        return {"generation": generation, "expires": self._clock() + self._ttl_s, "windows": windows}

    def _same_window(self, pending: dict, scene: object) -> bool:
        rows = scene.get("windows") if isinstance(scene, dict) else None
        if not isinstance(rows, list):
            return False
        matches = [row for row in rows if isinstance(row, dict) and row.get("handle") == pending["handle"]]
        if len(matches) != 1:
            return False
        row = matches[0]
        title = str(row.get("title", ""))
        image = str(row.get("image", ""))
        class_name = str(row.get("class_name", ""))
        if row.get("protected") or _protected(title, image, class_name):
            return False
        if image != _ALLOWED_IMAGE or class_name != _ALLOWED_CLASS:
            return False
        return row.get("title") == pending["title"] and row.get("pid") == pending["pid"]

    def _allowed(self, generation: int, deadline: float) -> bool:
        return self._halt.is_current(generation) and self._enabled() and self._clock() < deadline

    def _blocked_locked(self, generation: int, deadline: float) -> dict:
        if not self._halt.is_current(generation):
            self._notice = "The action was cancelled."
            self._audit.write(status="window_switch_cancelled", outcome="cancelled", detail="Window action cancelled")
        elif not self._enabled():
            self._notice = "Window switching is not enabled."
            self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window switching is not enabled")
        elif self._clock() >= deadline:
            self._notice = "That approval has expired."
            self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window action expired")
        else:
            self._notice = "The window changed before it could be focused."
            self._audit.write(status="window_switch_rejected", outcome="rejected", detail="Window target changed")
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
        observation = self._observation
        if observation is not None and (not self._halt.is_current(observation["generation"]) or self._clock() >= observation["expires"]):
            self._observation = None
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
            "ok": self._pending is not None or performed or self._notice == "Notepad was focused.",
            "permission_enabled": self._enabled(),
            "text": self._notice,
            "pending": pending,
            "observation": _public_observation(self._observation),
            "performed": performed,
            "executed": False,
        }


def _request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _SwitchError("That window action is not valid.")
    if set(payload) - {"action", "window_id"}:
        raise _SwitchError("That window action is not valid.")
    action = payload.get("action")
    window_id = payload.get("window_id", "")
    if action not in _ACTIONS:
        raise _SwitchError("That window action is not allowed.")
    if not isinstance(window_id, str):
        raise _SwitchError("That window action is not valid.")
    if window_id and not _ID.fullmatch(window_id):
        raise _SwitchError("That window is not available.")
    if looks_like_secret(window_id):
        raise _SwitchError("That window action is not supported.")
    return {"action": action, "window_id": window_id}


def _accept_window(row: object) -> dict | None:
    if not isinstance(row, dict):
        return None
    image = row.get("image")
    class_name = row.get("class_name")
    title = row.get("title")
    if image != _ALLOWED_IMAGE or class_name != _ALLOWED_CLASS:
        return None
    if row.get("protected") or _protected(str(title), str(image), str(class_name)):
        return None
    if not isinstance(title, str) or not _TITLE.fullmatch(title) or looks_like_secret(title):
        return None
    handle = row.get("handle")
    pid = row.get("pid")
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return {"handle": handle, "pid": pid, "title": title, "application": _ALLOWED_NAME}


def _pending(action: str, summary: str, handle: int, window_id: str, title: str, pid: int) -> dict:
    return {"action": action, "summary": summary, "handle": handle, "window_id": window_id, "title": title, "pid": pid}


def _public_observation(observation: dict | None) -> dict | None:
    if observation is None:
        return None
    windows = []
    for window in observation["windows"].values():
        windows.append({"window_id": window["window_id"], "application": window["application"], "title": window["title"]})
    return {"windows": windows}


def _protected(title: str, image: str, class_name: str) -> bool:
    folded = " ".join((title, image, class_name)).lower()
    markers = ("password", "credential", "user account control", "windows security", "bitlocker", "consent.exe", "sign in")
    return any(marker in folded for marker in markers)


def _focus_port():
    from jarvis.desktop.windows import focus_handle, list_notepad_identities

    class _Port:
        def observe(self) -> dict:
            return list_notepad_identities()

        def focus(self, handle: int, allow) -> bool:
            return focus_handle(handle, allow)

    return _Port()


def _monotonic() -> float:
    import time

    return time.monotonic()


class _SwitchError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
