import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.files import ListFiles
from jarvis.skills.registry import SkillRegistry


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    (notes / "inner").mkdir()
    (notes / "a.txt").write_text("hidden contents", encoding="utf-8")
    (notes / "inner" / "deep.txt").write_text("deep contents", encoding="utf-8")
    (workspace / "note.txt").write_text("hello workspace", encoding="utf-8")
    return workspace


def _executor(tmp_path: Path, workspace: Path):
    halt = HaltController()
    skill = ListFiles(workspace, halt)
    registry = SkillRegistry()
    registry.register(skill)
    policy = Policy(tmp_path)
    policy.enabled[Permission.LIST] = True
    executor = SkillExecutor(registry, policy, halt, AuditLog(tmp_path / "audit.jsonl"))
    return skill, executor, halt, policy


def _approve(executor: SkillExecutor, path: str):
    pending = executor.submit("list_files", {"path": path})
    assert pending.outcome == "needs_approval"
    return executor.confirm(pending.action_id, approved=True)


def test_list_shows_only_immediate_relative_names(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill, executor, halt, policy = _executor(tmp_path, workspace)
    policy.enabled[Permission.LIST] = False
    disabled = executor.submit("list_files", {"path": ""})
    assert disabled.outcome == "disabled"
    assert "note.txt" not in disabled.text
    policy.enabled[Permission.LIST] = True

    root = _approve(executor, "")
    assert root.ok is True
    assert root.text.splitlines() == ["note.txt", "notes/"]
    assert "hidden contents" not in root.text
    assert "a.txt" not in root.text
    assert str(tmp_path) not in root.text

    nested = skill.execute({"path": "notes"})
    assert nested.text.splitlines() == ["a.txt", "inner/"]
    assert "deep.txt" not in nested.text
    assert "deep contents" not in nested.text


def test_list_rejects_escape_and_symlink_targets(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill, executor, halt, policy = _executor(tmp_path, workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    for raw in ("../outside", "notes/../../outside", str(outside), "missing-dir"):
        result = skill.execute({"path": raw})
        assert result.ok is False
        assert "secret" not in result.text
        assert str(outside) not in result.text
    assert skill.execute({"path": "note.txt"}).error_type == "not_a_folder"
    assert skill.execute({"path": "missing-dir"}).error_type == "not_found"
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    assert skill.execute({"path": "escape"}).error_type == "outside_workspace"
    listed = skill.execute({"path": ""})
    assert "escape" in listed.text
    assert str(outside) not in listed.text
    assert "secret.txt" not in listed.text


def test_list_approval_is_one_folder_and_stop_does_not_replay(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill, executor, halt, policy = _executor(tmp_path, workspace)
    root = executor.submit("list_files", {"path": ""})
    nested = executor.submit("list_files", {"path": "notes"})
    denied = executor.confirm(nested.action_id, approved=False)
    assert denied.outcome == "denied"
    assert "a.txt" not in denied.text
    opened = executor.confirm(root.action_id, approved=True)
    assert opened.text.splitlines() == ["note.txt", "notes/"]
    assert "a.txt" not in opened.text
    again = executor.confirm(nested.action_id, approved=True)
    assert again.ok is False
    assert "a.txt" not in again.text

    waiting = executor.submit("list_files", {"path": "notes"})
    halt.emergency_stop()
    direct = skill.execute({"path": "notes"})
    assert direct.outcome == "cancelled"
    assert direct.text == ""
    halt.resume()
    replayed = executor.confirm(waiting.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert "a.txt" not in replayed.text


def _app(tmp_path: Path):
    return create_app(
        settings=Settings(
            provider="fake",
            groq_api_key="",
            groq_model="openai/gpt-oss-20b",
            gemini_api_key="",
            gemini_model="gemini-2.5-flash",
            timeout_s=5,
            max_steps=4,
            tool_timeout_s=20,
            host="127.0.0.1",
            port=8765,
            data_dir=tmp_path / "data",
            root=tmp_path,
        ),
        fake=FakeProvider(reply="local reply"),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
    )


def test_list_api_and_page_keep_read_file_separate(tmp_path: Path):
    _workspace(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Enable List Files" in page
        assert "Request listing" in page
        shown = page.split("function showListResult", 1)[1].split("async function decideList", 1)[0]
        assert "textContent" in shown
        assert "innerHTML" not in shown

        off = client.post("/api/skills/list", json={"path": ""}).json()
        assert off["outcome"] == "disabled"
        assert "note.txt" not in off["text"]
        enabled = client.post("/api/skills/list/permission", json={"enabled": True}).json()
        assert enabled == {"permission": "list", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["list"] is True
        assert permissions["read"] is False
        assert client.post("/api/skills/read", json={"path": "note.txt"}).json()["outcome"] == "disabled"

        requested = client.post("/api/skills/list", json={"path": ""}).json()
        assert requested["outcome"] == "needs_approval"
        assert requested["path"] == ""
        pending = client.get("/api/skills/list").json()["pending"]
        assert pending[0]["skill"] == "list_files"
        assert pending[0]["path"] == ""
        opened = client.post("/api/skills/list/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        assert opened["text"].splitlines() == ["note.txt", "notes/"]
        assert str(tmp_path) not in json.dumps(opened)
        assert "hidden contents" not in opened["text"]

        other = client.post("/api/skills/list", json={"path": "notes"}).json()
        crossed = client.post("/api/skills/read/confirm", json={"action_id": other["action_id"], "approved": True}).json()
        assert crossed["outcome"] == "rejected"
        assert "a.txt" not in crossed["text"]
        denied = client.post("/api/skills/list/confirm", json={"action_id": other["action_id"], "approved": False}).json()
        assert denied["outcome"] == "denied"
        assert "a.txt" not in denied["text"]

        live = client.post("/api/skills/list", json={"path": "notes"}).json()
        client.post("/api/emergency-stop")
        cancelled = client.post("/api/skills/list/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert cancelled["outcome"] == "cancelled"
        assert cancelled["text"] == ""
        assert client.post("/api/resume").json()["replayed"] is False
        assert client.get("/api/skills/list").json()["pending"] == []
        replayed = client.post("/api/skills/list/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert "a.txt" not in replayed["text"]
