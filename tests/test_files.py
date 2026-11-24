from pathlib import Path

import pytest

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.base import SkillResult
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.files import MAX_FILE_BYTES, ReadFile
from jarvis.skills.registry import SkillRegistry


def _reader(tmp_path: Path) -> tuple[Path, ReadFile, SkillExecutor, HaltController]:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    halt = HaltController()
    skill = ReadFile(workspace, halt)
    registry = SkillRegistry()
    registry.register(skill)
    policy = Policy(tmp_path)
    policy.enabled[Permission.READ] = True
    executor = SkillExecutor(registry, policy, halt, AuditLog(tmp_path / "audit.jsonl"))
    return workspace, skill, executor, halt


def _approve(executor: SkillExecutor, path: str) -> SkillResult:
    pending = executor.submit("read_file", {"path": path})
    assert pending.outcome == "needs_approval"
    return executor.confirm(pending.action_id, approved=True)


def test_read_returns_text_only_after_that_action_is_approved(tmp_path: Path):
    workspace, skill, executor, halt = _reader(tmp_path)
    (workspace / "note.txt").write_text("hello workspace", encoding="utf-8")
    (workspace / "note.md").write_text("import os\nos.system('echo pwned')\n", encoding="utf-8")
    marker = tmp_path / "pwned.txt"
    pending = executor.submit("read_file", {"path": "note.txt"})
    assert pending.outcome == "needs_approval"
    assert pending.text == "That action needs approval."
    assert "hello workspace" not in pending.text
    other = executor.submit("read_file", {"path": "note.md"})
    opened = executor.confirm(pending.action_id, approved=True)
    assert opened.ok is True
    assert opened.text == "hello workspace"
    assert skill.execute({"path": "note.md"}).text.startswith("import os")
    assert not marker.exists()
    denied = executor.confirm(other.action_id, approved=False)
    assert denied.outcome == "denied"
    again = _approve(executor, "note.md")
    assert "os.system" in again.text
    assert not marker.exists()


def test_read_rejects_escape_and_unsupported_files(tmp_path: Path):
    workspace, skill, executor, halt = _reader(tmp_path)
    (workspace / "note.txt").write_text("inside", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    nested = workspace / "notes"
    nested.mkdir()
    (nested / "a.md").write_text("nested", encoding="utf-8")
    (workspace / "code.py").write_text("print(1)", encoding="utf-8")
    (workspace / "folder.txt").mkdir()

    assert _approve(executor, "notes/a.md").text == "nested"
    assert _approve(executor, str(workspace / "note.txt")).text == "inside"

    for raw in ("../secret.txt", str(secret), "notes/../../secret.txt", "", "note.txt\x00.md"):
        result = skill.execute({"path": raw})
        assert result.ok is False
        assert result.error_type == "outside_workspace"
        assert "secret" not in result.text

    unsupported = skill.execute({"path": "code.py"})
    assert unsupported.error_type == "unsupported_type"
    assert "print(1)" not in unsupported.text
    missing = skill.execute({"path": "missing.txt"})
    assert missing.error_type == "not_found"
    assert skill.execute({"path": "folder.txt"}).error_type == "not_a_file"


def test_read_rejects_symlink_escape_and_large_files(tmp_path: Path):
    workspace, skill, executor, halt = _reader(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is not available")
    escaped = skill.execute({"path": "link.txt"})
    assert escaped.error_type == "outside_workspace"
    assert escaped.text == "That file is outside the workspace."

    huge = workspace / "huge.txt"
    huge.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    too_big = skill.execute({"path": "huge.txt"})
    assert too_big.error_type == "too_large"
    assert "x" * 20 not in too_big.text
    (workspace / "edge.txt").write_bytes(b"y" * MAX_FILE_BYTES)
    assert skill.execute({"path": "edge.txt"}).text == "y" * MAX_FILE_BYTES


def test_stop_cancels_a_pending_read_and_resume_does_not_replay(tmp_path: Path):
    workspace, skill, executor, halt = _reader(tmp_path)
    (workspace / "note.txt").write_text("hello workspace", encoding="utf-8")
    pending = executor.submit("read_file", {"path": "note.txt"})
    halt.emergency_stop()
    direct = skill.execute({"path": "note.txt"})
    assert direct.outcome == "cancelled"
    assert direct.text == ""
    halt.resume()
    replayed = executor.confirm(pending.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert replayed.text == ""
    assert "hello workspace" not in replayed.text
