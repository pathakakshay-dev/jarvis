"""Approved, target-bound desktop actions.

The planner, simulator, goal agent, and Notepad opener are not used here.
Model text is never treated as an action.
"""

from __future__ import annotations

import re
import threading
import uuid

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.policy import Permission, Policy
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_ID = re.compile(r"^[0-9a-f]{32}$")
_TITLE = re.compile(r"^\*?[A-Za-z0-9][A-Za-z0-9 .,'-]{0,119}$")
_TEXT = re.compile(r"^[A-Za-z0-9 .,!?'-]{1,200}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'-]{0,79}$")
_ACTIONS = frozenset({"observe", "focus_window", "type_text", "click_element", "key_shortcut"})
_SHORTCUTS = frozenset({"ctrl+a"})
_ALLOWED_IMAGE = "notepad.exe"
_ALLOWED_CLASS = "Notepad"
_ALLOWED_NAME = "Notepad"


class DesktopController:
    """Preview one desktop action, then perform it only after its own approval."""

    def __init__(self, halt: HaltController, audit: AuditLog, policy: Policy, *, desktop=None, clock=None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid desktop control lifetime")
        self._halt = halt
        self._audit = audit
        self._policy = policy
        self._desktop = desktop if desktop is not None else _windows_port()
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
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
            self._policy.enabled[Permission.AUTOMATE] = bool(enabled)
            return self._enabled()

    def preview(self, payload: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The action was cancelled.")
            if not self._enabled():
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop control is not enabled")
                return self._stop_view("Desktop control is not enabled.")
            if self._pending is not None:
                return self._stop_view("A desktop action is already waiting.")
            try:
                request = _request(payload)
            except _ControlError as exc:
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop action rejected")
                return self._stop_view(exc.text)
            try:
                pending = self._bind_locked(request)
            except _ControlError as exc:
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop action rejected")
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
            self._audit.write(status="desktop_control_preview", outcome="pending", detail="Desktop action previewed")
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            self._expire_locked()
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop action rejected")
                return self._stop_view("That action is not waiting for approval.")
            pending = self._pending
            if pending is None or pending["action_id"] != action_id:
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop action rejected")
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._audit.write(status="desktop_control_cancelled", outcome="cancelled", detail="Desktop action cancelled")
                return self._stop_view("The action was cancelled.")
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="desktop_control_denied", outcome="denied", detail="Desktop action denied")
                self._notice = "The action was denied."
                return self._view_locked(False)
            if not self._enabled():
                self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop control is not enabled")
                return self._stop_view("Desktop control is not enabled.")
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                self._audit.write(status="desktop_control_cancelled", outcome="cancelled", detail="Desktop action cancelled")
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
                    self._notice = "The desktop could not be inspected."
                outcome = "completed" if self._observation is not None else "rejected"
                self._audit.write(status="desktop_control_" + outcome, outcome=outcome, detail="Desktop inspection finished")
                return self._view_locked(self._observation is not None)
            if performed:
                self._audit.write(status="desktop_control_completed", outcome="completed", detail="Desktop action completed")
                self._notice = _done_text(pending["action"])
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
            self._audit.write(status="desktop_control_cancelled", outcome="cancelled", detail="Desktop action cancelled")
            return self._view_locked(False)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._pending = None
                self._observation = None
                self._notice = ""

    def _perform_locked(self, pending: dict, generation: int, deadline: float) -> bool:
        allow = lambda: self._allowed(generation, deadline)
        action = pending["action"]
        if action == "observe":
            if not allow():
                return False
            scene = self._desktop.observe()
            if not allow():
                self._observation = None
                return False
            self._observation = self._accept_scene(scene, generation)
            return True
        fresh = self._desktop.observe()
        if not allow() or not self._same_target(pending, fresh):
            return False
        handle = pending["element_handle"] or pending["handle"]
        if action == "focus_window":
            return bool(self._desktop.focus(pending["handle"], allow))
        if action == "type_text":
            return bool(self._desktop.type_text(handle, pending["text"], allow))
        if action == "click_element":
            return bool(self._desktop.click(handle, allow))
        if action == "key_shortcut":
            return bool(self._desktop.shortcut(handle, allow))
        return False

    def _bind_locked(self, request: dict) -> dict:
        action = request["action"]
        if action == "open_application":
            raise _ControlError("Opening an application uses its own approved action.")
        if action == "observe":
            if request["window_id"] or request["element_id"] or request["text"] or request["shortcut"]:
                raise _ControlError("That desktop action is not valid.")
            return _pending(action, "List open Notepad windows.", 0, 0, "", "", "", "", 0)
        observation = self._observation
        if observation is None or not self._halt.is_current(observation["generation"]) or self._clock() >= observation["expires"]:
            raise _ControlError("Inspect the window before choosing an action.")
        window = observation["windows"].get(request["window_id"])
        if window is None:
            raise _ControlError("That desktop target is not allowed.")
        element = None
        if action != "focus_window":
            element = observation["elements"].get(request["element_id"])
            if element is None or element["window_id"] != window["window_id"]:
                raise _ControlError("That desktop target is not allowed.")
            if element["password"]:
                raise _ControlError("That field is protected.")
        if action == "focus_window":
            if request["element_id"] or request["text"] or request["shortcut"]:
                raise _ControlError("That desktop action is not valid.")
            return _pending(action, "Focus " + window["title"] + ".", window["handle"], 0, window["window_id"], "", window["title"], "", window["pid"])
        if action == "type_text":
            if request["shortcut"] or element["role"] != "edit" or not _TEXT.fullmatch(request["text"]):
                raise _ControlError("That desktop action is not valid.")
            summary = "Type into the Notepad text editor."
            return _pending(action, summary, window["handle"], element["handle"], window["window_id"], element["element_id"], window["title"], request["text"], window["pid"])
        if action == "click_element":
            if request["text"] or request["shortcut"] or element["role"] != "button":
                raise _ControlError("That desktop action is not valid.")
            return _pending(action, "Click the identified Notepad button.", window["handle"], element["handle"], window["window_id"], element["element_id"], window["title"], "", window["pid"])
        if action == "key_shortcut":
            if request["text"] or request["shortcut"] not in _SHORTCUTS or element["role"] != "edit":
                raise _ControlError("That desktop action is not valid.")
            return _pending(action, "Select all text in the Notepad editor.", window["handle"], element["handle"], window["window_id"], element["element_id"], window["title"], "", window["pid"])
        raise _ControlError("That desktop action is not allowed.")

    def _accept_scene(self, scene: object, generation: int) -> dict | None:
        rows = scene.get("windows") if isinstance(scene, dict) else None
        if not isinstance(rows, list):
            self._notice = "No Notepad window was found."
            return None
        windows: dict[str, dict] = {}
        elements: dict[str, dict] = {}
        for row in rows:
            built = _accept_window(row)
            if built is None:
                continue
            window_id = uuid.uuid4().hex
            window, window_elements = built
            window["window_id"] = window_id
            windows[window_id] = window
            for element in window_elements:
                element_id = uuid.uuid4().hex
                element["element_id"] = element_id
                element["window_id"] = window_id
                elements[element_id] = element
        if not windows:
            self._notice = "No Notepad window was found."
            return None
        self._notice = "Choose a Notepad window."
        return {
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
            "windows": windows,
            "elements": elements,
        }

    def _same_target(self, pending: dict, scene: object) -> bool:
        rows = scene.get("windows") if isinstance(scene, dict) else None
        if not isinstance(rows, list):
            return False
        matches = [row for row in rows if isinstance(row, dict) and row.get("handle") == pending["handle"]]
        if len(matches) != 1:
            return False
        row = matches[0]
        if row.get("protected") or _protected_text(str(row.get("title", "")), str(row.get("image", "")), str(row.get("class_name", ""))):
            return False
        if row.get("image") != _ALLOWED_IMAGE or row.get("class_name") != _ALLOWED_CLASS:
            return False
        if row.get("title") != pending["title"] or row.get("pid") != pending["pid"]:
            return False
        if pending["action"] == "focus_window":
            return True
        elements = row.get("elements")
        if not isinstance(elements, list):
            return False
        found = [
            element
            for element in elements
            if isinstance(element, dict)
            and element.get("handle") == pending["element_handle"]
            and element.get("password") is False
            and element.get("role") == _expected_role(pending["action"])
        ]
        return len(found) == 1

    def _allowed(self, generation: int, deadline: float) -> bool:
        return self._halt.is_current(generation) and self._enabled() and self._clock() < deadline

    def _blocked_locked(self, generation: int, deadline: float) -> dict:
        if not self._halt.is_current(generation):
            self._notice = "The action was cancelled."
            self._audit.write(status="desktop_control_cancelled", outcome="cancelled", detail="Desktop action cancelled")
        elif not self._enabled():
            self._notice = "Desktop control is not enabled."
            self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop control is not enabled")
        elif self._clock() >= deadline:
            self._notice = "That approval has expired."
            self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop action expired")
        else:
            self._notice = "The desktop changed before the action."
            self._audit.write(status="desktop_control_rejected", outcome="rejected", detail="Desktop target changed")
        return self._view_locked(False)

    def _consume_locked(self, action_id: str) -> None:
        self._used.add(action_id)
        if self._pending is not None and self._pending["action_id"] == action_id:
            self._pending = None

    def _expire_locked(self) -> None:
        pending = self._pending
        if pending is not None and (not self._halt.is_current(pending["generation"]) or self._clock() >= pending["expires"]):
            action_id = pending["action_id"]
            if self._halt.approval_state(action_id) == "pending":
                self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)
            self._notice = "The action was cancelled."
        observation = self._observation
        if observation is not None and (not self._halt.is_current(observation["generation"]) or self._clock() >= observation["expires"]):
            self._observation = None

    def _enabled(self) -> bool:
        return bool(self._policy.enabled[Permission.AUTOMATE])

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
                "text": self._pending["text"],
                "state": "waiting for approval",
            }
        observation = _public_observation(self._observation)
        return {
            "ok": self._pending is not None or performed or self._notice in {"The Notepad window was inspected.", "Text was typed.", "Notepad was focused.", "The button was clicked.", "The editor text was selected."},
            "permission_enabled": self._enabled(),
            "text": self._notice,
            "pending": pending,
            "observation": observation,
            "performed": performed,
            "executed": False,
        }


def _request(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _ControlError("That desktop action is not valid.")
    allowed = {"action", "window_id", "element_id", "text", "shortcut"}
    if set(payload) - allowed:
        raise _ControlError("That desktop action is not valid.")
    action = payload.get("action")
    window_id = payload.get("window_id", "")
    element_id = payload.get("element_id", "")
    text = payload.get("text", "")
    shortcut = payload.get("shortcut", "")
    if action == "open_application":
        raise _ControlError("Opening an application uses its own approved action.")
    if action not in _ACTIONS:
        raise _ControlError("That desktop action is not allowed.")
    if not isinstance(window_id, str) or not isinstance(element_id, str) or not isinstance(text, str) or not isinstance(shortcut, str):
        raise _ControlError("That desktop action is not valid.")
    if window_id and not _ID.fullmatch(window_id):
        raise _ControlError("That desktop target is not allowed.")
    if element_id and not _ID.fullmatch(element_id):
        raise _ControlError("That desktop target is not allowed.")
    if looks_like_secret(text) or looks_like_secret(shortcut):
        raise _ControlError("That desktop action is not supported.")
    if shortcut:
        shortcut = shortcut.strip().lower()
    return {"action": action, "window_id": window_id, "element_id": element_id, "text": text, "shortcut": shortcut}


def _accept_window(row: object) -> tuple[dict, list[dict]] | None:
    if not isinstance(row, dict):
        return None
    image = row.get("image")
    class_name = row.get("class_name")
    title = row.get("title")
    if image != _ALLOWED_IMAGE or class_name != _ALLOWED_CLASS:
        return None
    if row.get("protected") or _protected_text(str(title), str(image), str(class_name)):
        return None
    if not isinstance(title, str) or not _TITLE.fullmatch(title) or looks_like_secret(title):
        return None
    handle = row.get("handle")
    pid = row.get("pid")
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    raw_elements = row.get("elements")
    if not isinstance(raw_elements, list):
        return None
    elements = []
    for item in raw_elements:
        if not isinstance(item, dict):
            return None
        element_handle = item.get("handle")
        role = item.get("role")
        password = item.get("password")
        name = item.get("name")
        if not isinstance(element_handle, int) or isinstance(element_handle, bool) or element_handle <= 0:
            return None
        if role not in {"edit", "button"} or type(password) is not bool:
            return None
        if password:
            continue
        if role == "edit":
            name = "Text editor"
        elif not isinstance(name, str) or name == "":
            name = "Button"
        elif not _NAME.fullmatch(name) or looks_like_secret(name):
            return None
        elements.append({"handle": element_handle, "role": role, "name": name, "password": False})
    return (
        {"handle": handle, "pid": pid, "title": title, "application": _ALLOWED_NAME},
        elements,
    )


def _pending(action: str, summary: str, handle: int, element_handle: int, window_id: str, element_id: str, title: str, text: str, pid: int) -> dict:
    return {
        "action": action,
        "summary": summary,
        "handle": handle,
        "element_handle": element_handle,
        "window_id": window_id,
        "element_id": element_id,
        "title": title,
        "text": text,
        "pid": pid,
        "shortcut": "ctrl+a" if action == "key_shortcut" else "",
    }


def _public_observation(observation: dict | None) -> dict | None:
    if observation is None:
        return None
    windows = []
    for window in observation["windows"].values():
        elements = []
        for element in observation["elements"].values():
            if element["window_id"] != window["window_id"] or element["password"]:
                continue
            elements.append(
                {
                    "element_id": element["element_id"],
                    "window_id": window["window_id"],
                    "name": element["name"],
                    "role": element["role"],
                }
            )
        windows.append(
            {
                "window_id": window["window_id"],
                "application": window["application"],
                "title": window["title"],
                "elements": elements,
            }
        )
    return {"windows": windows}


def _expected_role(action: str) -> str:
    if action == "click_element":
        return "button"
    return "edit"


def _done_text(action: str) -> str:
    if action == "observe":
        return "The Notepad window was inspected."
    if action == "focus_window":
        return "Notepad was focused."
    if action == "type_text":
        return "Text was typed."
    if action == "click_element":
        return "The button was clicked."
    if action == "key_shortcut":
        return "The editor text was selected."
    return "The action was not performed."


def _protected_text(title: str, image: str, class_name: str) -> bool:
    folded = " ".join((title, image, class_name)).lower()
    return any(marker in folded for marker in ("password", "credential", "user account control", "windows security", "bitlocker", "consent.exe", "sign in"))


def _windows_port():
    from jarvis.desktop.windows import click_handle, focus_handle, list_notepad_windows, select_all_handle, type_into_handle

    class _Port:
        def observe(self) -> dict:
            return list_notepad_windows()

        def focus(self, handle: int, allow) -> bool:
            return focus_handle(handle, allow)

        def type_text(self, handle: int, text: str, allow) -> bool:
            return type_into_handle(handle, text, allow)

        def click(self, handle: int, allow) -> bool:
            return click_handle(handle, allow)

        def shortcut(self, handle: int, allow) -> bool:
            return select_all_handle(handle, allow)

    return _Port()


def _monotonic() -> float:
    import time

    return time.monotonic()


class _ControlError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
