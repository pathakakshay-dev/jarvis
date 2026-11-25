"""Read-only Windows system summary. It does not run commands or open user files."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import sys
import time
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
_DRIVE = re.compile(r"[A-Za-z]:")


class _Stopped(Exception):
    pass


class SystemInfo:
    spec = SkillSpec(
        id="system_info",
        description="Report the Windows version, CPU, memory, system drive, battery, and uptime.",
        permission=Permission.SYSTEM,
        parameters=_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, halt: HaltController, reader=None) -> None:
        self._halt = halt
        self._reader = reader or collect_system_report

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = self._reader(self._halt)
        except _Stopped:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        except Exception:
            return SkillResult(False, text="System information is unavailable.", outcome="rejected", error_type="unavailable")
        if self._halt.halted or not isinstance(text, str):
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def collect_system_report(halt: HaltController) -> str:
    if halt.halted:
        raise _Stopped()
    if sys.platform != "win32":
        return _report(
            [
                ("Operating system", "unavailable"),
                ("CPU", "unavailable"),
                ("Logical processors", "unavailable"),
                ("CPU usage", "unavailable"),
                ("Memory", "unavailable"),
                ("System drive", "unavailable"),
                ("Battery", "unavailable"),
                ("Uptime", "unavailable"),
            ]
        )
    cpu_name, cpu_count = _cpu_identity()
    usage = _cpu_usage(halt)
    if halt.halted:
        raise _Stopped()
    total, available = _memory()
    drive, free, capacity = _disk()
    battery = _battery()
    uptime = _uptime()
    memory = "unavailable"
    if total is not None and available is not None:
        memory = _gib(available) + " available of " + _gib(total)
    disk = "unavailable"
    if drive and free is not None and capacity is not None:
        disk = drive + " " + _gib(free) + " free of " + _gib(capacity)
    return _report(
        [
            ("Operating system", _os_version()),
            ("CPU", cpu_name),
            ("Logical processors", "unavailable" if cpu_count is None else str(cpu_count)),
            ("CPU usage", "unavailable" if usage is None else str(usage) + "%"),
            ("Memory", memory),
            ("System drive", disk),
            ("Battery", battery),
            ("Uptime", uptime),
        ]
    )


def _report(rows: list[tuple[str, str]]) -> str:
    return "\n".join(label + ": " + value for label, value in rows)


def _os_version() -> str:
    try:
        release, version, _csd, _ptype = platform.win32_ver()
    except Exception:
        return "unavailable"
    release = release.strip()
    version = version.strip()
    if not release and not version:
        return "unavailable"
    return " ".join(part for part in ("Windows", release, version) if part)


def _cpu_identity() -> tuple[str, int | None]:
    try:
        name = " ".join(platform.processor().split())
    except Exception:
        name = ""
    try:
        count = os.cpu_count()
    except Exception:
        count = None
    if not isinstance(count, int) or count < 1:
        count = None
    return name or "unavailable", count


def _cpu_usage(halt: HaltController) -> int | None:
    first = _system_times()
    if first is None:
        return None
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        if halt.halted:
            raise _Stopped()
        time.sleep(0.05)
    if halt.halted:
        raise _Stopped()
    second = _system_times()
    if second is None:
        return None
    idle = second[0] - first[0]
    total = (second[1] - first[1]) + (second[2] - first[2])
    if total <= 0:
        return None
    busy = max(0, total - idle)
    return max(0, min(100, round(100 * busy / total)))


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.GetSystemTimes.argtypes = (
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    )
    library.GetSystemTimes.restype = wintypes.BOOL
    library.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MEMORYSTATUSEX),)
    library.GlobalMemoryStatusEx.restype = wintypes.BOOL
    library.GetSystemPowerStatus.argtypes = (ctypes.POINTER(_POWER),)
    library.GetSystemPowerStatus.restype = wintypes.BOOL
    library.GetTickCount64.argtypes = ()
    library.GetTickCount64.restype = ctypes.c_ulonglong
    return library


def _system_times() -> tuple[int, int, int] | None:
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    try:
        ok = _kernel32().GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    except Exception:
        return None
    if not ok:
        return None
    return _ticks(idle), _ticks(kernel), _ticks(user)


def _memory() -> tuple[int | None, int | None]:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    try:
        ok = _kernel32().GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:
        return None, None
    if not ok:
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _disk() -> tuple[str, int | None, int | None]:
    raw = os.environ.get("SystemDrive", "C:")
    if not isinstance(raw, str) or not _DRIVE.fullmatch(raw):
        return "", None, None
    try:
        usage = shutil.disk_usage(raw + "\\")
    except OSError:
        return raw.upper(), None, None
    return raw.upper(), int(usage.free), int(usage.total)


def _battery() -> str:
    status = _POWER()
    try:
        ok = _kernel32().GetSystemPowerStatus(ctypes.byref(status))
    except Exception:
        return "unavailable"
    if not ok:
        return "unavailable"
    if int(status.BatteryFlag) & 128:
        return "unavailable"
    percent = int(status.BatteryLifePercent)
    if percent < 0 or percent > 100:
        return "unavailable"
    line = str(percent) + "%"
    ac = int(status.ACLineStatus)
    if ac == 1:
        line += ", plugged in"
    elif ac == 0:
        line += ", on battery"
    return line


def _uptime() -> str:
    try:
        ticks = int(_kernel32().GetTickCount64())
    except Exception:
        return "unavailable"
    if ticks < 0:
        return "unavailable"
    seconds = ticks // 1000
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(str(days) + "d")
    parts.append(str(hours) + "h")
    parts.append(str(minutes) + "m")
    return " ".join(parts)


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GB"


def _ticks(value: "_FILETIME") -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


class _FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = (
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


class _POWER(ctypes.Structure):
    _fields_ = (
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    )
