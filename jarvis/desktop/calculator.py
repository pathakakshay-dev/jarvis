"""Open Calculator only. This module cannot start any other program."""

from __future__ import annotations

import ctypes
import re
import threading
import uuid
from ctypes import wintypes
from dataclasses import dataclass

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError

_ACTION = "Open Calculator"
_APPLICATION = "Calculator"
_FILE_NAME = "calc.exe"
_TTL_S = 120.0
_ID = re.compile(r"^[0-9a-f]{32}$")
_SYSTEM_LEAVES = frozenset({"system32", "syswow64", "sysnative"})


@dataclass
class _Pending:
    action_id: str
    generation: int
    expires_at: float


class CalculatorOpener:
    """One real action: open Calculator after a fresh explicit approval."""

    def __init__(self, halt: HaltController, audit: AuditLog, *, launcher=None, clock=None, ttl_s: float = _TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("invalid calculator approval lifetime")
        self._halt = halt
        self._audit = audit
        self._launcher = launcher or self._production_launch
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled_flag = False
        self._pending: dict[str, _Pending] = {}
        self._used: set[str] = set()
        self._detail = ""
        self._lock = threading.RLock()

    def status(self) -> dict:
        with self._lock:
            self._sync_locked()
            pending = self._pending_view_locked()
            return {
                "permission_enabled": self._enabled(),
                "action": _ACTION,
                "application": _APPLICATION,
                "pending": pending,
                "launched": False,
                "detail": self._detail,
            }

    def request(self, payload: object = None) -> dict:
        with self._lock:
            self._sync_locked()
            if payload not in (None, {}):
                self._audit.write(status="calculator_rejected", outcome="rejected", error_type="unexpected", detail="Calculator open rejected")
                return self._reply(False, "That action is not allowed.", launched=False)
            if self._halt.halted:
                return self._reply(False, "The action was cancelled.", launched=False)
            if not self._enabled():
                self._audit.write(status="calculator_rejected", outcome="rejected", error_type="disabled", detail="Open Calculator is not enabled")
                return self._reply(False, "Open Calculator is not enabled.", launched=False)
            if self._pending:
                return self._reply(False, "An Open Calculator action is already waiting.", launched=False)
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return self._reply(False, "The action was cancelled.", launched=False)
            action_id = uuid.uuid4().hex
            if action_id in self._used or self._halt.approval_state(action_id) is not None:
                return self._reply(False, "That action is not allowed.", launched=False)
            item = _Pending(action_id, generation, self._clock() + self._ttl_s)
            self._pending[action_id] = item
            try:
                self._halt.request_approval(action_id)
            except HaltedError:
                self._pending.pop(action_id, None)
                self._used.add(action_id)
                return self._reply(False, "The action was cancelled.", launched=False)
            if not self._halt.is_current(generation) or self._halt.approval_state(action_id) != "pending":
                self._pending.pop(action_id, None)
                self._used.add(action_id)
                return self._reply(False, "The action was cancelled.", launched=False)
            self._audit.write(status="calculator_requested", outcome="pending", detail="Calculator open requested")
            return self._reply(
                False,
                "Open Calculator needs approval.",
                launched=False,
                pending={
                    "action_id": action_id,
                    "action": _ACTION,
                    "application": _APPLICATION,
                    "state": "pending",
                },
            )

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            if isinstance(action_id, str) and _ID.fullmatch(action_id):
                waiting = self._pending.get(action_id)
                if (
                    waiting is not None
                    and self._clock() >= waiting.expires_at
                    and self._halt.is_current(waiting.generation)
                    and self._halt.approval_state(action_id) == "pending"
                ):
                    self._consume_locked(action_id)
                    self._block_locked(action_id)
                    self._audit.write(status="calculator_rejected", outcome="rejected", error_type="expired", detail="Calculator open expired")
                    return self._reply(False, "That approval has expired.", launched=False)
            self._sync_locked()
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="calculator_rejected", outcome="rejected", error_type="forged_id", detail="Calculator open rejected")
                return self._reply(False, "That action is not waiting for approval.", launched=False)
            item = self._pending.get(action_id)
            if item is None:
                self._audit.write(status="calculator_rejected", outcome="rejected", error_type="forged_id", detail="Calculator open rejected")
                return self._reply(False, "That action is not waiting for approval.", launched=False)
            if not self._halt.is_current(item.generation) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                self._audit.write(status="calculator_cancelled", outcome="cancelled", detail="Calculator open cancelled")
                return self._reply(False, "The action was cancelled.", launched=False)
            if type(approved) is not bool:
                return self._reply(False, "That action is not waiting for approval.", launched=False)
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="calculator_denied", outcome="denied", detail="Calculator open denied")
                return self._reply(False, "The action was denied.", launched=False)
            if not self._enabled():
                self._audit.write(status="calculator_rejected", outcome="rejected", error_type="disabled", detail="Open Calculator is not enabled")
                return self._reply(False, "Open Calculator is not enabled.", launched=False)
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                self._audit.write(status="calculator_cancelled", outcome="cancelled", detail="Calculator open cancelled")
                return self._reply(False, "The action was cancelled.", launched=False)
            if not self._halt.is_current(item.generation) or not self._enabled():
                self._consume_locked(action_id)
                self._audit.write(status="calculator_cancelled", outcome="cancelled", detail="Calculator open cancelled")
                return self._reply(False, "The action was cancelled.", launched=False)
            generation = item.generation
            self._consume_locked(action_id)
            if not self._launch_allowed_locked(generation):
                return self._blocked_launch_locked(generation)
            try:
                launched = bool(self._launcher(generation))
            except Exception:
                launched = False
            if launched:
                self._audit.write(status="calculator_opened", outcome="completed", detail="Calculator opened")
                return self._reply(True, "Calculator was opened.", launched=True)
            return self._blocked_launch_locked(generation)

    def observe_stop(self) -> None:
        with self._lock:
            self._sync_locked()
            if self._halt.halted:
                self._detail = ""

    def set_enabled(self, enabled: bool) -> bool:
        """Toggle Open Calculator under the same lock as the launch boundary."""
        with self._lock:
            self._enabled_flag = bool(enabled)
            return self._enabled()

    def _reply(self, ok: bool, detail: str, *, launched: bool, pending: dict | None = None) -> dict:
        self._detail = detail
        body = _result(ok, detail, launched=launched)
        if pending is not None:
            body["pending"] = pending
        return body

    def _production_launch(self, generation: int) -> bool:
        return launch_calculator(allow=lambda: self._launch_allowed_locked(generation))

    def _launch_allowed_locked(self, generation: int) -> bool:
        """Final authorization: this generation is current and Open Calculator is enabled."""
        return self._halt.is_current(generation) and self._enabled()

    def _blocked_launch_locked(self, generation: int) -> dict:
        if not self._halt.is_current(generation):
            self._audit.write(status="calculator_cancelled", outcome="cancelled", detail="Calculator open cancelled")
            return self._reply(False, "The action was cancelled.", launched=False)
        if not self._enabled():
            self._audit.write(status="calculator_rejected", outcome="rejected", error_type="disabled", detail="Open Calculator is not enabled")
            return self._reply(False, "Open Calculator is not enabled.", launched=False)
        self._audit.write(status="calculator_rejected", outcome="rejected", error_type="failed", detail="Calculator was not opened")
        return self._reply(False, "Calculator was not opened.", launched=False)

    def _enabled(self) -> bool:
        return self._enabled_flag

    def _consume_locked(self, action_id: str) -> None:
        self._pending.pop(action_id, None)
        self._used.add(action_id)

    def _block_locked(self, action_id: str) -> None:
        if self._halt.halted:
            return
        if self._halt.approval_state(action_id) == "pending":
            self._halt.resolve_approval(action_id, "denied")

    def _sync_locked(self) -> None:
        now = self._clock()
        for action_id, item in list(self._pending.items()):
            expired = now >= item.expires_at
            stale = (not self._halt.is_current(item.generation)) or self._halt.approval_state(action_id) != "pending"
            if not expired and not stale:
                continue
            self._consume_locked(action_id)
            if expired and not stale:
                self._block_locked(action_id)

    def _pending_view_locked(self) -> dict | None:
        if not self._pending:
            return None
        item = next(iter(self._pending.values()))
        if self._clock() >= item.expires_at or not self._halt.is_current(item.generation):
            return None
        return {
            "action_id": item.action_id,
            "action": _ACTION,
            "application": _APPLICATION,
            "state": "pending",
        }


def calculator_launch_path(system_directory: object) -> str | None:
    """Return the system Calculator path, or nothing if the directory is not usable."""
    if not isinstance(system_directory, str) or not system_directory or "\x00" in system_directory:
        return None
    if any(ord(character) < 32 for character in system_directory):
        return None
    if any(token in system_directory for token in ("..", "/", '"', "%", ";", "|", "&", " ", "?", "*", "<", ">", "'", "`")):
        return None
    directory = system_directory.rstrip("\\")
    if directory.count(":") != 1 or len(directory) < 3 or directory[1] != ":":
        return None
    if ".exe" in directory.lower() or directory.endswith(":"):
        return None
    leaf = directory.rsplit("\\", 1)[-1].lower()
    if leaf not in _SYSTEM_LEAVES:
        return None
    path = directory + "\\" + _FILE_NAME
    if path.count("\\" + _FILE_NAME) != 1 or not path.endswith("\\" + _FILE_NAME):
        return None
    return path


def launch_calculator(*, system_directory: str | None = None, starter=None, allow=None) -> bool:
    """Start the system Calculator binary. A blocked or non-matching path starts nothing."""
    if allow is not None and not allow():
        return False
    # A caller-supplied directory is ignored unless a test double replaces process creation.
    directory = system_directory if starter is not None else read_system_directory()
    if starter is not None and system_directory is None:
        return False
    path = calculator_launch_path(directory)
    if path is None:
        return False
    if allow is not None and not allow():
        return False
    start = starter or _create_process
    try:
        if starter is None:
            return bool(_create_process(path, allow=allow))
        if allow is not None and not allow():
            return False
        return bool(start(path))
    except Exception:
        return False


def read_system_directory() -> str:
    buffer = ctypes.create_unicode_buffer(260)
    length = _kernel32().GetSystemDirectoryW(buffer, 260)
    if not length or length >= 260:
        return ""
    return buffer.value


def _keep_created_process(allow, terminate) -> bool:
    """Decide whether the process created by this call stays running.

    terminate is invoked only for that new process. It is not a search for
    other Calculator windows. If terminate fails, the process is still running
    and the launch is reported as started. A successful terminate request
    does not wait for the process to exit.
    """
    if allow is None or allow():
        return True
    return not bool(terminate())


def _create_process(path: str, *, allow=None) -> bool:
    if not isinstance(path, str) or not path.endswith("\\" + _FILE_NAME):
        return False
    directory, separator, name = path.rpartition("\\")
    expected = calculator_launch_path(read_system_directory())
    if separator != "\\" or name != _FILE_NAME or expected is None or path != expected:
        return False
    if allow is not None and not allow():
        return False
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    process = _PROCESS_INFORMATION()
    created = bool(
        _kernel32().CreateProcessW(
            path,
            None,
            None,
            None,
            False,
            0,
            None,
            directory,
            ctypes.byref(startup),
            ctypes.byref(process),
        )
    )
    if not created:
        return False
    keep = _keep_created_process(allow, lambda: _kernel32().TerminateProcess(process.hProcess, 1))
    _kernel32().CloseHandle(process.hThread)
    _kernel32().CloseHandle(process.hProcess)
    return keep


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


_KERNEL = None


def _kernel32():
    global _KERNEL
    if _KERNEL is None:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        library.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        library.GetSystemDirectoryW.restype = wintypes.UINT
        library.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        library.CreateProcessW.restype = wintypes.BOOL
        library.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        library.TerminateProcess.restype = wintypes.BOOL
        library.CloseHandle.argtypes = [wintypes.HANDLE]
        library.CloseHandle.restype = wintypes.BOOL
        _KERNEL = library
    return _KERNEL


def _result(ok: bool, detail: str, *, launched: bool) -> dict:
    return {
        "ok": ok,
        "detail": detail,
        "action": _ACTION,
        "application": _APPLICATION,
        "launched": launched,
    }


def _monotonic() -> float:
    import time

    return time.monotonic()
