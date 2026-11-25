"""Windows-native browser window reads for allowlisted Edge and Chrome.

This module does not connect to DevTools, open ports, launch browsers, or
read page content. It only describes top-level window identity and title.
"""

from __future__ import annotations

import re
import sys

_CHROME_CLASS = "Chrome_WidgetWin_1"
_ALLOWED = {
    "chrome.exe": {"browser": "Google Chrome", "suffix": " - Google Chrome"},
    "msedge.exe": {"browser": "Microsoft Edge", "suffix": " - Microsoft Edge"},
}
_MORE_TABS = re.compile(r"^(?P<title>.+?) and (?P<count>\d+) more pages$", re.IGNORECASE)
_MAX_WINDOWS = 200
_MAX_SESSIONS = 8
_MAX_TITLE = 120
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class BrowserUnavailable(Exception):
    pass


def list_browser_sessions() -> dict:
    """Return allowlisted browser windows only. Other windows are not described."""
    if sys.platform != "win32":
        raise BrowserUnavailable()
    user32, kernel32 = _libraries()
    found: list[int] = []

    def _enum(hwnd, _lparam) -> bool:
        if len(found) >= _MAX_WINDOWS:
            return False
        found.append(int(hwnd))
        return True

    callback = _callback_type()(_enum)
    user32.EnumWindows(callback, 0)
    rows = []
    for hwnd in found:
        if len(rows) >= _MAX_SESSIONS:
            break
        if not user32.IsWindowVisible(hwnd):
            continue
        row = _describe(user32, kernel32, hwnd)
        if row is None:
            continue
        rows.append(row)
    return {"sessions": visible_browser_sessions(rows)}


def read_browser_session(handle: int, pid: int, image: str, class_name: str, title: str) -> dict | None:
    """Re-read one allowlisted browser window. Fail closed on mismatch."""
    if sys.platform != "win32":
        raise BrowserUnavailable()
    if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if image not in _ALLOWED or class_name != _CHROME_CLASS:
        return None
    user32, kernel32 = _libraries()
    if not user32.IsWindow(handle) or not user32.IsWindowVisible(handle):
        return None
    fresh = _describe(user32, kernel32, handle)
    if fresh is None:
        return None
    if fresh["handle"] != handle or fresh["pid"] != pid or fresh["image"] != image or fresh["class_name"] != class_name:
        return None
    if fresh["title"] != title:
        return None
    return inspection_from_session(fresh)


def visible_browser_sessions(rows: object) -> list[dict]:
    """Keep safe allowlisted browser rows. Other titles are dropped."""
    if not isinstance(rows, list):
        return []
    visible = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        image = row.get("image")
        class_name = row.get("class_name")
        if image not in _ALLOWED or class_name != _CHROME_CLASS:
            continue
        if row.get("protected") or _protected(str(image), str(class_name), str(row.get("title", ""))):
            continue
        title = row.get("title")
        if not isinstance(title, str) or not title or len(title) > _MAX_TITLE:
            continue
        handle = row.get("handle")
        pid = row.get("pid")
        if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
            continue
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        visible.append(
            {
                "handle": handle,
                "pid": pid,
                "image": image,
                "class_name": _CHROME_CLASS,
                "title": title,
                "browser": _ALLOWED[image]["browser"],
                "protected": False,
            }
        )
        if len(visible) >= _MAX_SESSIONS:
            break
    return visible


def inspection_from_session(session: object) -> dict | None:
    """Build a local inspection record. URL is never filled from this path."""
    if not isinstance(session, dict):
        return None
    image = session.get("image")
    if image not in _ALLOWED or session.get("class_name") != _CHROME_CLASS:
        return None
    title = session.get("title")
    if not isinstance(title, str) or not title or len(title) > _MAX_TITLE:
        return None
    if session.get("protected") or _protected(str(image), _CHROME_CLASS, title):
        return None
    tab_title, tab_count = _parse_tab(title, _ALLOWED[image]["suffix"])
    if tab_title is None:
        return None
    return {
        "browser": _ALLOWED[image]["browser"],
        "image": image,
        "tab_title": tab_title,
        "url": "",
        "url_available": False,
        "tab_count": tab_count,
        "window_title": title,
    }


def _parse_tab(title: str, suffix: str) -> tuple[str | None, int]:
    if not title.endswith(suffix):
        return None, 0
    body = title[: -len(suffix)].strip()
    if not body or len(body) > _MAX_TITLE:
        return None, 0
    match = _MORE_TABS.fullmatch(body)
    if match is not None:
        count = int(match.group("count"))
        if count < 1 or count > 99:
            return None, 0
        page = match.group("title").strip()
        if not page or len(page) > _MAX_TITLE:
            return None, 0
        return page, count + 1
    return body, 1


def _describe(user32, kernel32, hwnd: int) -> dict | None:
    image = _image_name(user32, kernel32, hwnd)
    class_name = _class_name(user32, hwnd)
    if image not in _ALLOWED or class_name != _CHROME_CLASS:
        return None
    title = _window_text(user32, hwnd)
    if not title:
        return None
    protected = _protected(image, class_name, title)
    return {
        "handle": hwnd,
        "pid": _process_id(user32, hwnd),
        "image": image,
        "class_name": class_name,
        "title": "" if protected else title[:_MAX_TITLE],
        "browser": _ALLOWED[image]["browser"],
        "protected": protected,
    }


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
        "login",
    )
    return any(marker in folded for marker in markers)


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

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
    user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindow.restype = ctypes.wintypes.BOOL
    return user32, kernel32


def _callback_type():
    import ctypes
    from ctypes import wintypes

    return ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
