"""Permission switches and the approved-folder path jail."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from jarvis.skills.base import SkillSpec

SHELL_IDS = frozenset({"shell", "cmd", "powershell", "exec", "command", "bash"})


class Permission(str, Enum):
    READ = "read"
    LIST = "list"
    SEARCH = "search"
    WRITE = "write"
    FOLDER = "folder"
    SYSTEM = "system"
    APPS = "apps"
    CLIPBOARD = "clipboard"
    DELETE = "delete"
    SEND = "send"
    APP_CONTROL = "app_control"
    AUTOMATE = "automate"


SENSITIVE = frozenset(
    {
        Permission.WRITE,
        Permission.FOLDER,
        Permission.DELETE,
        Permission.SEND,
        Permission.APP_CONTROL,
        Permission.AUTOMATE,
    }
)


class Policy:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.enabled = {permission: False for permission in Permission}

    def decide(self, spec: SkillSpec) -> str:
        if not isinstance(spec.permission, Permission):
            raise ValueError("unknown permission")
        if not self.enabled[spec.permission]:
            return "disabled"
        if spec.permission in SENSITIVE:
            return "needs_approval"
        return "allowed"


def assert_skill_allowed(spec: SkillSpec) -> None:
    if spec.id.strip().lower() in SHELL_IDS:
        raise ValueError("shell execution is disabled")
    if not isinstance(spec.permission, Permission):
        raise ValueError("unknown permission")


def resolve_inside(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PermissionError("invalid path")
    root_resolved = root.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PermissionError("outside approved root") from exc
    return resolved
