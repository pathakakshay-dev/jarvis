"""Sequential Desktop Action Engine for approved Notepad actions.

This engine queues supported actions, previews each one, and performs it only
after its own approval. It does not launch programs, and it does not use the
goal agent, planner, simulator, Notepad opener, Calculator opener, or desktop
control controller.
"""

from __future__ import annotations

import re
import threading
import uuid

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

_TTL_S = 120.0
_MAX_STEPS = 6
_ID = re.compile(r"^[0-9a-f]{32}$")
_TITLE = re.compile(r"^\*?[A-Za-z0-9][A-Za-z0-9 .,'-]{0,119}$")
_TEXT = re.compile(r"^[A-Za-z0-9 .,!?'-]{1,200}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'-]{0,79}$")
_ACTIONS = frozenset({"observe", "focus_window", "type_text", "click_element", "key_shortcut"})
_SHORTCUTS = frozenset({"ctrl+a"})
_ALLOWED_IMAGE = "notepad.exe"
_ALLOWED_CLASS = "Notepad"
_ALLOWED_NAME = "Notepad"
_RUN_DONE = frozenset({"completed", "failed", "stopped", "cancelled", "denied"})


class DesktopActionEngine:
    """Propose a short Notepad action sequence and approve each step once."""

    def __init__(self, halt: HaltController, audit: AuditLog, *, desktop=None, clock=None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid desktop engine lifetime")
        self._halt = halt
        self._audit = audit
        self._desktop = desktop if desktop is not None else _windows_port()
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled_flag = False
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._observation: dict | None = None
        self._run: dict | None = None
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

    def propose(self, payload: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The action was cancelled.")
            if not self._enabled():
                self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action engine is not enabled")
                return self._stop_view("Desktop action engine is not enabled.")
            if self._pending is not None or (self._run is not None and self._run["status"] not in _RUN_DONE):
                return self._stop_view("A desktop task is already running.")
            try:
                steps = _steps(payload)
            except _EngineError as exc:
                self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop task rejected")
                return self._stop_view(exc.text)
            if any(step["action"] != "observe" for step in steps):
                if self._observation is None or not self._halt.is_current(self._observation["generation"]) or self._clock() >= self._observation["expires"]:
                    return self._stop_view("Inspect the window before choosing an action.")
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._stop_view("The action was cancelled.")
            plan_id = uuid.uuid4().hex
            prepared = []
            for step in steps:
                try:
                    prepared.append(self._prepare_step_locked(step))
                except _EngineError as exc:
                    self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop task rejected")
                    return self._stop_view(exc.text)
            self._run = {
                "plan_id": plan_id,
                "generation": generation,
                "status": "proposed",
                "index": 0,
                "steps": prepared,
                "expires": self._clock() + self._ttl_s,
            }
            self._notice = "Review the desktop task."
            self._audit.write(status="desktop_engine_proposed", outcome="pending", detail="Desktop task proposed")
            return self._view_locked(False)

    def start(self, plan_id: object, approved: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._stop_view("The action was cancelled.")
            if not self._enabled():
                return self._stop_view("Desktop action engine is not enabled.")
            run = self._run
            if run is None or not isinstance(plan_id, str) or plan_id != run["plan_id"]:
                return self._stop_view("That desktop task is not waiting.")
            if run["status"] != "proposed":
                return self._view_locked(False)
            if type(approved) is not bool or not approved:
                self._finish_locked("denied", "The task was denied.")
                self._audit.write(status="desktop_engine_denied", outcome="denied", detail="Desktop task denied")
                return self._view_locked(False)
            if not self._halt.is_current(run["generation"]):
                self._finish_locked("stopped", "The action was cancelled.")
                return self._view_locked(False)
            try:
                self._arm_step_locked()
            except _EngineError as exc:
                self._finish_locked("failed", exc.text)
                self._audit.write(status="desktop_engine_failed", outcome="failed", detail="Desktop task failed")
                return self._view_locked(False)
            self._audit.write(status="desktop_engine_started", outcome="pending", detail="Desktop task started")
            return self._view_locked(False)

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked(False)
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action rejected")
                return self._stop_view("That action is not waiting for approval.")
            pending = self._pending
            run = self._run
            if pending is None or run is None or pending["action_id"] != action_id:
                self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action rejected")
                return self._stop_view("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._finish_locked("stopped", "The action was cancelled.")
                self._audit.write(status="desktop_engine_cancelled", outcome="cancelled", detail="Desktop action cancelled")
                return self._view_locked(False)
            if type(approved) is not bool:
                return self._stop_view("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._mark_step_locked("denied")
                self._finish_locked("denied", "The action was denied.")
                self._audit.write(status="desktop_engine_denied", outcome="denied", detail="Desktop action denied")
                return self._view_locked(False)
            if not self._enabled():
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._finish_locked("failed", "Desktop action engine is not enabled.")
                self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action engine is not enabled")
                return self._view_locked(False)
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                self._finish_locked("stopped", "The action was cancelled.")
                self._audit.write(status="desktop_engine_cancelled", outcome="cancelled", detail="Desktop action cancelled")
                return self._view_locked(False)
            generation = pending["generation"]
            deadline = pending["expires"]
            self._consume_locked(action_id)
            if not self._allowed(generation, deadline):
                return self._blocked_locked(generation, deadline)
            try:
                performed = self._perform_locked(pending, generation, deadline)
                verified = performed and self._verify_locked(pending, generation, deadline)
            except Exception:
                performed = False
                verified = False
            if performed and verified:
                self._mark_step_locked("completed")
                self._audit.write(status="desktop_engine_step", outcome="completed", detail="Desktop step completed")
                if run["index"] + 1 >= len(run["steps"]):
                    done_text = self._notice if pending["action"] == "observe" and self._notice else "The desktop task finished."
                    self._finish_locked("completed", done_text)
                    self._audit.write(status="desktop_engine_completed", outcome="completed", detail="Desktop task finished")
                    return self._view_locked(True)
                run["index"] += 1
                run["status"] = "waiting"
                try:
                    self._arm_step_locked()
                except _EngineError as exc:
                    self._finish_locked("failed", exc.text)
                    self._audit.write(status="desktop_engine_failed", outcome="failed", detail="Desktop task failed")
                    return self._view_locked(False)
                return self._view_locked(True)
            return self._blocked_locked(generation, deadline, performed=performed)

    def cancel(self) -> dict:
        with self._lock:
            self._expire_locked()
            pending = self._pending
            if pending is not None:
                action_id = pending["action_id"]
                if self._halt.approval_state(action_id) == "pending":
                    self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
            if self._run is not None and self._run["status"] not in _RUN_DONE:
                self._finish_locked("cancelled", "The action was cancelled.")
            else:
                self._notice = "The action was cancelled."
            self._audit.write(status="desktop_engine_cancelled", outcome="cancelled", detail="Desktop task cancelled")
            return self._view_locked(False)

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._pending = None
                self._observation = None
                if self._run is not None and self._run["status"] not in _RUN_DONE:
                    self._run["status"] = "stopped"
                    for step in self._run["steps"][self._run["index"] :]:
                        if step["state"] in {"pending", "waiting"}:
                            step["state"] = "stopped"
                self._notice = ""

    def _prepare_step_locked(self, step: dict) -> dict:
        action = step["action"]
        if action == "observe":
            if step["window_id"] or step["element_id"] or step["text"] or step["shortcut"]:
                raise _EngineError("That desktop action is not valid.")
            return {
                "action": action,
                "summary": "List open Notepad windows.",
                "window_id": "",
                "element_id": "",
                "text": "",
                "shortcut": "",
                "handle": 0,
                "element_handle": 0,
                "title": "",
                "pid": 0,
                "state": "pending",
            }
        observation = self._observation
        if observation is None:
            raise _EngineError("Inspect the window before choosing an action.")
        window = observation["windows"].get(step["window_id"])
        if window is None:
            raise _EngineError("That desktop target is not allowed.")
        element = None
        if action != "focus_window":
            element = observation["elements"].get(step["element_id"])
            if element is None or element["window_id"] != window["window_id"]:
                raise _EngineError("That desktop target is not allowed.")
            if element["password"]:
                raise _EngineError("That field is protected.")
        if action == "focus_window":
            if step["element_id"] or step["text"] or step["shortcut"]:
                raise _EngineError("That desktop action is not valid.")
            summary = "Focus " + window["title"] + "."
            return _step_row(action, summary, window, None, "", "")
        if action == "type_text":
            if step["shortcut"] or element["role"] != "edit" or not _TEXT.fullmatch(step["text"]):
                raise _EngineError("That desktop action is not valid.")
            return _step_row(action, "Type into the Notepad text editor.", window, element, step["text"], "")
        if action == "click_element":
            if step["text"] or step["shortcut"] or element["role"] != "button":
                raise _EngineError("That desktop action is not valid.")
            return _step_row(action, "Click the identified Notepad button.", window, element, "", "")
        if action == "key_shortcut":
            if step["text"] or step["shortcut"] not in _SHORTCUTS or element["role"] != "edit":
                raise _EngineError("That desktop action is not valid.")
            return _step_row(action, "Select all text in the Notepad editor.", window, element, "", "ctrl+a")
        raise _EngineError("That desktop action is not allowed.")

    def _arm_step_locked(self) -> None:
        run = self._run
        if run is None:
            raise _EngineError("That desktop task is not waiting.")
        if self._pending is not None:
            raise _EngineError("A desktop action is already waiting.")
        if run["index"] >= len(run["steps"]):
            raise _EngineError("That desktop task is not waiting.")
        step = run["steps"][run["index"]]
        action_id = uuid.uuid4().hex
        if action_id in self._used or self._halt.approval_state(action_id) is not None:
            raise _EngineError("That action is not allowed.")
        generation = run["generation"]
        if not self._halt.is_current(generation):
            raise _EngineError("The action was cancelled.")
        pending = {
            "action_id": action_id,
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
            "action": step["action"],
            "summary": step["summary"],
            "handle": step["handle"],
            "element_handle": step["element_handle"],
            "window_id": step["window_id"],
            "element_id": step["element_id"],
            "title": step["title"],
            "text": step["text"],
            "pid": step["pid"],
            "shortcut": step["shortcut"],
        }
        try:
            self._halt.request_approval(action_id)
        except HaltedError as exc:
            self._used.add(action_id)
            raise _EngineError("The action was cancelled.") from exc
        if not self._halt.is_current(generation) or self._halt.approval_state(action_id) != "pending":
            self._used.add(action_id)
            raise _EngineError("The action was cancelled.")
        step["state"] = "waiting"
        run["status"] = "waiting"
        self._pending = pending
        self._notice = step["summary"]

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

    def _verify_locked(self, pending: dict, generation: int, deadline: float) -> bool:
        if not self._allowed(generation, deadline):
            return False
        if pending["action"] == "observe":
            return True
        fresh = self._desktop.observe()
        if not self._allowed(generation, deadline):
            return False
        return self._same_target(pending, fresh)

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
        if row.get("protected") or _protected(str(row.get("title", "")), str(row.get("image", "")), str(row.get("class_name", ""))):
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

    def _blocked_locked(self, generation: int, deadline: float, *, performed: bool = False) -> dict:
        if not self._halt.is_current(generation):
            self._finish_locked("stopped", "The action was cancelled.")
            self._audit.write(status="desktop_engine_cancelled", outcome="cancelled", detail="Desktop action cancelled")
        elif not self._enabled():
            self._finish_locked("failed", "Desktop action engine is not enabled.")
            self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action engine is not enabled")
        elif self._clock() >= deadline:
            self._finish_locked("failed", "That approval has expired.")
            self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop action expired")
        elif performed:
            self._mark_step_locked("failed")
            self._finish_locked("failed", "The desktop action could not be verified.")
            self._audit.write(status="desktop_engine_failed", outcome="failed", detail="Desktop verification failed")
        else:
            self._mark_step_locked("failed")
            self._finish_locked("failed", "The desktop changed before the action.")
            self._audit.write(status="desktop_engine_rejected", outcome="rejected", detail="Desktop target changed")
        return self._view_locked(False)

    def _mark_step_locked(self, state: str) -> None:
        run = self._run
        if run is None or run["index"] >= len(run["steps"]):
            return
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
                elif step["state"] in {"pending", "waiting"}:
                    step["state"] = "skipped"
        self._pending = None
        self._notice = notice

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
            if self._halt.is_current(pending["generation"]) and self._clock() >= pending["expires"]:
                self._finish_locked("failed", "That approval has expired.")
            else:
                self._finish_locked("stopped", "The action was cancelled.")
            expired = action_id
        run = self._run
        if run is not None and run["status"] == "proposed" and (not self._halt.is_current(run["generation"]) or self._clock() >= run["expires"]):
            self._finish_locked("failed" if self._halt.is_current(run["generation"]) else "stopped", "That approval has expired." if self._halt.is_current(run["generation"]) else "The action was cancelled.")
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
                "text": self._pending["text"],
                "state": "waiting for approval",
            }
        run = None
        can_start = False
        if self._run is not None:
            run = {
                "plan_id": self._run["plan_id"],
                "status": self._run["status"],
                "index": self._run["index"],
                "steps": [
                    {
                        "action": step["action"],
                        "summary": step["summary"],
                        "state": step["state"],
                    }
                    for step in self._run["steps"]
                ],
            }
            can_start = self._run["status"] == "proposed"
        return {
            "ok": self._pending is not None or performed or (self._run is not None and self._run["status"] == "completed"),
            "permission_enabled": self._enabled(),
            "text": self._notice,
            "pending": pending,
            "observation": _public_observation(self._observation),
            "task": run,
            "can_start": can_start,
            "performed": performed,
            "executed": False,
        }


def _steps(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise _EngineError("That desktop task is not valid.")
    if set(payload) - {"steps"}:
        raise _EngineError("That desktop task is not valid.")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > _MAX_STEPS:
        raise _EngineError("That desktop task is not valid.")
    cleaned = []
    for item in steps:
        if not isinstance(item, dict):
            raise _EngineError("That desktop task is not valid.")
        allowed = {"action", "window_id", "element_id", "text", "shortcut"}
        if set(item) - allowed:
            raise _EngineError("That desktop task is not valid.")
        action = item.get("action")
        window_id = item.get("window_id", "")
        element_id = item.get("element_id", "")
        text = item.get("text", "")
        shortcut = item.get("shortcut", "")
        if action == "open_application":
            raise _EngineError("Opening an application uses its own approved action.")
        if action not in _ACTIONS:
            raise _EngineError("That desktop action is not allowed.")
        if not isinstance(window_id, str) or not isinstance(element_id, str) or not isinstance(text, str) or not isinstance(shortcut, str):
            raise _EngineError("That desktop action is not valid.")
        if window_id and not _ID.fullmatch(window_id):
            raise _EngineError("That desktop target is not allowed.")
        if element_id and not _ID.fullmatch(element_id):
            raise _EngineError("That desktop target is not allowed.")
        if looks_like_secret(text) or looks_like_secret(shortcut):
            raise _EngineError("That desktop action is not supported.")
        if shortcut:
            shortcut = shortcut.strip().lower()
        cleaned.append({"action": action, "window_id": window_id, "element_id": element_id, "text": text, "shortcut": shortcut})
    return cleaned


def _accept_window(row: object) -> tuple[dict, list[dict]] | None:
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
    return ({"handle": handle, "pid": pid, "title": title, "application": _ALLOWED_NAME}, elements)


def _step_row(action: str, summary: str, window: dict, element: dict | None, text: str, shortcut: str) -> dict:
    return {
        "action": action,
        "summary": summary,
        "window_id": window["window_id"],
        "element_id": "" if element is None else element["element_id"],
        "text": text,
        "shortcut": shortcut,
        "handle": window["handle"],
        "element_handle": 0 if element is None else element["handle"],
        "title": window["title"],
        "pid": window["pid"],
        "state": "pending",
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


def _protected(title: str, image: str, class_name: str) -> bool:
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


class _EngineError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
