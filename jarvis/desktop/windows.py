"""Windows-native window reads and target-bound messages.

This module does not start programs, send shell commands, or move the mouse.
Every change is sent to one window handle and only after allow() is still true.
"""

from __future__ import annotations

import sys

_EDIT = "Edit"
_RICH_EDIT = "RichEditD2DPT"
_BUTTON = "Button"
_NOTEPAD_CLASS = "Notepad"
_NOTEPAD_IMAGE = "notepad.exe"
_CHILD_SCAN_LIMIT = 64
_ELEMENT_LIMIT = 12
_PASSWORD_STYLE = 0x0020
_WM_CHAR = 0x0102
_BM_CLICK = 0x00F5
_EM_SETSEL = 0x00B1
_GWL_STYLE = -16
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class DesktopUnavailable(Exception):
    pass


def list_notepad_windows() -> dict:
    """Return allowlisted Notepad windows only. Other windows are not described."""
    if sys.platform != "win32":
        raise DesktopUnavailable()
    user32, kernel32 = _libraries()
    found: list[int] = []

    def _enum(hwnd, _lparam) -> bool:
        if len(found) >= 200:
            return False
        found.append(int(hwnd))
        return True

    callback = _callback_type()(_enum)
    user32.EnumWindows(callback, 0)
    described = []
    for hwnd in found:
        if len(described) >= 8:
            break
        row = _describe(user32, kernel32, hwnd)
        if row["class_name"] == _NOTEPAD_CLASS and row["image"] == _NOTEPAD_IMAGE:
            described.append(row)
    return {"windows": visible_notepad_windows(described)}


def list_notepad_identities() -> dict:
    """Notepad window identity only. Child controls and their text are omitted."""
    listed = list_notepad_windows()
    identities = []
    for row in listed.get("windows", []):
        if not isinstance(row, dict):
            continue
        identities.append(
            {
                "handle": row.get("handle"),
                "pid": row.get("pid"),
                "image": row.get("image"),
                "class_name": row.get("class_name"),
                "title": row.get("title"),
                "protected": False,
            }
        )
    return {"windows": identities}


def visible_notepad_windows(rows: object) -> list[dict]:
    """Keep safe Notepad rows. Titles from any other window are dropped."""
    if not isinstance(rows, list):
        return []
    visible = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("class_name") != _NOTEPAD_CLASS or row.get("image") != _NOTEPAD_IMAGE:
            continue
        if row.get("protected") or _protected(str(row.get("image", "")), str(row.get("class_name", "")), str(row.get("title", ""))):
            continue
        title = row.get("title")
        if not isinstance(title, str) or not title:
            continue
        visible.append(
            {
                "handle": row.get("handle"),
                "pid": row.get("pid"),
                "image": _NOTEPAD_IMAGE,
                "class_name": _NOTEPAD_CLASS,
                "title": title,
                "protected": False,
                "elements": row.get("elements") if isinstance(row.get("elements"), list) else [],
            }
        )
    return visible


def focus_handle(handle: int, allow) -> bool:
    if not allow():
        return False
    if sys.platform != "win32":
        return False
    user32, kernel32 = _libraries()
    hwnd = int(handle)
    if _image_name(user32, kernel32, hwnd) != _NOTEPAD_IMAGE or _class_name(user32, hwnd) != _NOTEPAD_CLASS:
        return False
    if not allow():
        return False
    return bool(user32.SetForegroundWindow(hwnd))


def type_into_handle(handle: int, text: str, allow) -> bool:
    if not allow() or not isinstance(text, str) or not text:
        return False
    if any(ord(char) < 32 or ord(char) > 126 for char in text):
        return False
    if sys.platform != "win32":
        return False
    user32, kernel32 = _libraries()
    hwnd = int(handle)
    if not _editable(user32, kernel32, hwnd) or not allow():
        return False
    sent = False
    for char in text:
        if not allow():
            return sent
        user32.SendMessageW(hwnd, _WM_CHAR, ord(char), 0)
        sent = True
    return sent


def click_handle(handle: int, allow) -> bool:
    if not allow():
        return False
    if sys.platform != "win32":
        return False
    user32, kernel32 = _libraries()
    hwnd = int(handle)
    if _class_name(user32, hwnd) != _BUTTON or _password(user32, hwnd) or not _notepad_owner(user32, kernel32, hwnd):
        return False
    if not allow():
        return False
    user32.SendMessageW(hwnd, _BM_CLICK, 0, 0)
    return True


def select_all_handle(handle: int, allow) -> bool:
    """Select the text in one edit control. This is not a global shortcut."""
    if not allow():
        return False
    if sys.platform != "win32":
        return False
    user32, kernel32 = _libraries()
    hwnd = int(handle)
    if not _editable(user32, kernel32, hwnd) or not allow():
        return False
    user32.SendMessageW(hwnd, _EM_SETSEL, 0, -1)
    return True


def _describe(user32, kernel32, hwnd: int) -> dict:
    image = _image_name(user32, kernel32, hwnd)
    class_name = _class_name(user32, hwnd)
    title = _window_text(user32, hwnd)
    protected = _protected(image, class_name, title)
    return {
        "handle": hwnd,
        "pid": _process_id(user32, hwnd),
        "image": image,
        "class_name": class_name,
        "title": "" if protected else title,
        "protected": protected,
        "elements": [] if protected else _elements(user32, kernel32, hwnd),
    }


def _elements(user32, kernel32, parent: int) -> list[dict]:
    found: list[int] = []

    def _collect(hwnd, _lparam) -> bool:
        if len(found) >= _CHILD_SCAN_LIMIT:
            return False
        found.append(int(hwnd))
        return True

    callback_type = _callback_type()
    callback = callback_type(_collect)
    user32.EnumChildWindows(parent, callback, 0)
    children = []
    for hwnd in found:
        class_name = _class_name(user32, hwnd)
        password = _password(user32, hwnd)
        children.append(
            {
                "handle": hwnd,
                "class_name": class_name,
                "password": password,
                "notepad_owned": class_name == _RICH_EDIT and _notepad_owner(user32, kernel32, hwnd),
                "name": "" if password or class_name in {_EDIT, _RICH_EDIT} else _window_text(user32, hwnd),
            }
        )
    return _targets_from_children(children)


def _role_for(class_name: str, password: bool, notepad_owned: bool) -> str | None:
    """Classic Edit, or the Windows 11 Notepad editor when that process owns it."""
    if class_name == _EDIT or (class_name == _RICH_EDIT and notepad_owned):
        return "edit"
    if class_name == _BUTTON and not password:
        return "button"
    return None


def _targets_from_children(children: object) -> list[dict]:
    if not isinstance(children, list):
        return []
    rows = []
    for child in children[:_CHILD_SCAN_LIMIT]:
        if not isinstance(child, dict):
            continue
        class_name = child.get("class_name")
        if not isinstance(class_name, str):
            continue
        password = child.get("password") is True
        role = _role_for(class_name, password, child.get("notepad_owned") is True)
        if role is None:
            continue
        handle = child.get("handle")
        name = child.get("name")
        if not isinstance(name, str):
            name = ""
        rows.append(
            {
                "handle": handle,
                "class_name": class_name,
                "role": role,
                "name": name,
                "password": password,
            }
        )
        if len(rows) >= _ELEMENT_LIMIT:
            break
    return rows


def _editable(user32, kernel32, hwnd: int) -> bool:
    class_name = _class_name(user32, hwnd)
    if class_name != _EDIT and class_name != _RICH_EDIT:
        return False
    return not _password(user32, hwnd) and _notepad_owner(user32, kernel32, hwnd)


def _notepad_owner(user32, kernel32, hwnd: int) -> bool:
    root = int(user32.GetAncestor(hwnd, 2) or hwnd)
    return _image_name(user32, kernel32, root) == _NOTEPAD_IMAGE and _class_name(user32, root) == _NOTEPAD_CLASS


def _protected(image: str, class_name: str, title: str) -> bool:
    folded = " ".join((image, class_name, title)).lower()
    markers = (
        "consent.exe",
        "credential",
        "user account control",
        "windows security",
        "bitlocker",
        "password",
        "sign in",
    )
    return any(marker in folded for marker in markers)


def _password(user32, hwnd: int) -> bool:
    return bool(int(user32.GetWindowLongW(hwnd, _GWL_STYLE)) & _PASSWORD_STYLE)


def _process_id(user32, hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _image_name(user32, kernel32, hwnd: int) -> str:
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(process)


def _class_name(user32, hwnd: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buffer, 128)
    return buffer.value


def _window_text(user32, hwnd: int) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(160)
    user32.GetWindowTextW(hwnd, buffer, 160)
    return " ".join(buffer.value.split())


def _libraries():
    import ctypes

    return ctypes.windll.user32, ctypes.windll.kernel32


def _callback_type():
    import ctypes
    from ctypes import wintypes

    return ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
