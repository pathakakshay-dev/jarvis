"""Windows hotkeys on a thread in this process. No extra package and no service."""

from __future__ import annotations

import sys
import threading
from typing import Callable

from jarvis.safety.halt import HaltController

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_J = 0x4A
VK_K = 0x4B
WM_HOTKEY = 0x0312
STOP_ID = 1
KILL_ID = 2


def handle_hotkey(hotkey_id: int, halt: HaltController, end_process: Callable[[], None]) -> None:
    if hotkey_id == STOP_ID:
        halt.emergency_stop()
    elif hotkey_id == KILL_ID:
        end_process()


def start_hotkeys(halt: HaltController, end_process: Callable[[], None]) -> threading.Thread | None:
    if sys.platform != "win32":
        halt.hotkey_available = False
        halt.hotkey_note = "Global hotkeys are unavailable on this system. The Stop button still works."
        return None

    def run() -> None:
        import ctypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        mods = MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint),
                ("pt", POINT),
            ]

        ok_stop = user32.RegisterHotKey(None, STOP_ID, mods, VK_J)
        ok_kill = user32.RegisterHotKey(None, KILL_ID, mods, VK_K)
        halt.hotkey_available = bool(ok_stop)
        if not ok_stop:
            halt.hotkey_note = "Emergency Stop hotkey is unavailable. The Stop button still works."
        elif not ok_kill:
            halt.hotkey_note = (
                "End process hotkey is unavailable. The Stop button still works. "
                "Emergency Stop hotkey is Ctrl+Alt+Shift+J."
            )
        else:
            halt.hotkey_note = (
                "Emergency Stop: Ctrl+Alt+Shift+J. End process: Ctrl+Alt+Shift+K. "
                "The Stop button always works."
            )
        message = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0 or result == -1:
                break
            if message.message == WM_HOTKEY:
                handle_hotkey(int(message.wParam), halt, end_process)
        user32.UnregisterHotKey(None, STOP_ID)
        user32.UnregisterHotKey(None, KILL_ID)

    thread = threading.Thread(target=run, name="jarvis-hotkey", daemon=True)
    thread.start()
    return thread
