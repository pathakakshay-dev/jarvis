"""Read-only list of installed application display names. It does not launch or change them."""

from __future__ import annotations

import sys

from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission
from jarvis.skills.base import SkillResult, SkillSpec

_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_UNINSTALL_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_MAX_NAMES = 100
_MAX_SUBKEYS = 1000
_MAX_NAME_LENGTH = 100
_UNAVAILABLE = "Installed applications are unavailable."
_EMPTY = "No installed application names were found."


class _Stopped(Exception):
    pass


class AppInventory:
    spec = SkillSpec(
        id="app_inventory",
        description="List installed application display names from the Windows uninstall registry.",
        permission=Permission.APPS,
        parameters=_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, halt: HaltController, reader=None) -> None:
        self._halt = halt
        self._reader = reader or collect_app_inventory

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


def collect_app_inventory(halt: HaltController, registry=None) -> str:
    if halt.halted:
        raise _Stopped()
    if registry is None:
        if sys.platform != "win32":
            return _UNAVAILABLE
        import winreg

        registry = winreg
    opened = 0
    names: list[str] = []
    for hive, path, access in uninstall_locations(registry):
        if halt.halted:
            raise _Stopped()
        try:
            found = read_display_names(registry, hive, path, access, halt)
        except FileNotFoundError:
            continue
        except _Stopped:
            raise
        except OSError:
            continue
        opened += 1
        names.extend(found)
    if opened == 0:
        return _UNAVAILABLE
    return format_app_list(names)


def uninstall_locations(registry) -> tuple[tuple[int, str, int], ...]:
    read = registry.KEY_READ
    return (
        (registry.HKEY_LOCAL_MACHINE, _UNINSTALL_PATH, read | registry.KEY_WOW64_64KEY),
        (registry.HKEY_LOCAL_MACHINE, _UNINSTALL_PATH, read | registry.KEY_WOW64_32KEY),
        (registry.HKEY_CURRENT_USER, _UNINSTALL_PATH, read),
    )


def read_display_names(registry, hive: int, path: str, access: int, halt: HaltController) -> list[str]:
    write_bits = registry.KEY_SET_VALUE | registry.KEY_CREATE_SUB_KEY
    if access & write_bits:
        raise OSError("registry write access is not used")
    root = registry.OpenKey(hive, path, 0, access)
    try:
        count = int(registry.QueryInfoKey(root)[0])
        names: list[str] = []
        for index in range(min(count, _MAX_SUBKEYS)):
            if halt.halted:
                raise _Stopped()
            subkey_name = registry.EnumKey(root, index)
            if not isinstance(subkey_name, str) or not subkey_name or "\x00" in subkey_name:
                continue
            try:
                subkey = registry.OpenKey(root, subkey_name, 0, registry.KEY_READ)
            except OSError:
                continue
            try:
                value, kind = registry.QueryValueEx(subkey, "DisplayName")
            except OSError:
                continue
            finally:
                registry.CloseKey(subkey)
            if kind != registry.REG_SZ:
                continue
            cleaned = clean_display_name(value)
            if cleaned:
                names.append(cleaned)
        return names
    finally:
        registry.CloseKey(root)


def clean_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name or len(name) > _MAX_NAME_LENGTH:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return None
    lowered = name.casefold()
    if "\\" in name or "%" in name or "://" in lowered or ".exe" in lowered or lowered.startswith("msiexec"):
        return None
    return name


def format_app_list(names: list[object], limit: int = _MAX_NAMES) -> str:
    unique: dict[str, str] = {}
    for value in names:
        cleaned = clean_display_name(value)
        if cleaned is None:
            continue
        key = cleaned.casefold()
        current = unique.get(key)
        if current is None or (cleaned.casefold(), cleaned) < (current.casefold(), current):
            unique[key] = cleaned
    ordered = sorted(unique.values(), key=lambda item: (item.casefold(), item))
    shown = ordered[:limit]
    if not shown:
        return _EMPTY
    lines = ["Installed applications:", *shown]
    if len(ordered) > limit:
        lines.append(f"List limited to {limit} names.")
    return "\n".join(lines)
