"""Read the current Windows text clipboard once. It does not monitor or change the clipboard."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission
from jarvis.skills.base import SkillResult, SkillSpec

_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_CF_UNICODETEXT = 13
_MAX_CHARS = 5000
_EMPTY = "The clipboard is empty."
_NO_TEXT = "The clipboard has no text."
_UNAVAILABLE = "The clipboard is unavailable."
_LIMIT_NOTE = "Showing the first 5000 characters."


class _Stopped(Exception):
    pass


class ClipboardRead:
    spec = SkillSpec(
        id="clipboard_read",
        description="Read the current Windows text clipboard once after approval.",
        permission=Permission.CLIPBOARD,
        parameters=_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, halt: HaltController, reader=None) -> None:
        self._halt = halt
        self._reader = reader or read_clipboard_text

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = self._reader(self._halt)
        except _Stopped:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        except Exception:
            return SkillResult(False, text=_UNAVAILABLE, outcome="rejected", error_type="unavailable")
        if self._halt.halted or not isinstance(text, str):
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def read_clipboard_text(halt: HaltController, user32=None, kernel32=None, copy_text=None) -> str:
    if halt.halted:
        raise _Stopped()
    if user32 is None or kernel32 is None:
        if sys.platform != "win32":
            return _UNAVAILABLE
        user32, kernel32 = _windows_clipboard()
    if copy_text is None:
        copy_text = ctypes.wstring_at
    if not user32.OpenClipboard(None):
        return _UNAVAILABLE
    try:
        if halt.halted:
            raise _Stopped()
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return _NO_TEXT
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return _NO_TEXT
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return _UNAVAILABLE
        try:
            raw = copy_text(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
        if halt.halted:
            raise _Stopped()
        if not isinstance(raw, str):
            return _NO_TEXT
        return limit_clipboard_text(raw)
    finally:
        user32.CloseClipboard()


def limit_clipboard_text(value: str) -> str:
    if value == "":
        return _EMPTY
    if len(value) <= _MAX_CHARS:
        return value
    return value[:_MAX_CHARS] + "\n" + _LIMIT_NOTE


def _windows_clipboard():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = ()
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = (wintypes.UINT,)
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    return user32, kernel32
