from pathlib import Path

import pytest

from jarvis.safety.policy import Permission, Policy, resolve_inside
from jarvis.skills.base import SkillSpec
from jarvis.skills.registry import SkillRegistry


class Dummy:
    def __init__(self, spec: SkillSpec) -> None:
        self.spec = spec


def test_permissions_default_off_and_sensitive_needs_approval(tmp_path: Path):
    policy = Policy(tmp_path)
    assert all(enabled is False for enabled in policy.enabled.values())
    read_spec = SkillSpec("lookup", "read a note", Permission.READ)
    assert policy.decide(read_spec) == "disabled"
    policy.enabled[Permission.READ] = True
    assert policy.decide(read_spec) == "allowed"
    write_spec = SkillSpec("save", "write a note", Permission.WRITE)
    policy.enabled[Permission.WRITE] = True
    assert policy.decide(write_spec) == "needs_approval"


def test_shell_skill_is_rejected():
    registry = SkillRegistry()
    spec = SkillSpec("shell", "run a command", Permission.READ)
    with pytest.raises(ValueError, match="shell"):
        registry.register(Dummy(spec))
    assert registry.ids() == []


def test_unknown_permission_is_rejected():
    registry = SkillRegistry()
    spec = SkillSpec("notes", "notes", "shell")
    with pytest.raises(ValueError, match="unknown permission"):
        registry.register(Dummy(spec))


def test_path_jail_blocks_parent_and_absolute_escape(tmp_path: Path):
    root = tmp_path / "approved"
    root.mkdir()
    (root / "note.txt").write_text("ok", encoding="utf-8")
    assert resolve_inside(root, "note.txt") == (root / "note.txt").resolve()
    with pytest.raises(PermissionError):
        resolve_inside(root, "../outside.txt")
    with pytest.raises(PermissionError):
        resolve_inside(root, str(tmp_path / "outside.txt"))
    with pytest.raises(PermissionError):
        resolve_inside(root, "")


def test_path_jail_blocks_symlink_escape(tmp_path: Path):
    root = tmp_path / "approved"
    outside = tmp_path / "secret.txt"
    root.mkdir()
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(PermissionError):
        resolve_inside(root, "link.txt")
