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
from jarvis.skills.files import CreateFolder, ListFiles, ReadFile, SearchFiles, WriteFile
from jarvis.skills.registry import SkillRegistry


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "note.txt").write_text("original note", encoding="utf-8")
    return workspace


def _executor(tmp_path: Path, workspace: Path):
    halt = HaltController()
    registry = SkillRegistry()
    registry.register(ReadFile(workspace, halt))
    registry.register(ListFiles(workspace, halt))
    registry.register(SearchFiles(workspace, halt))
    registry.register(WriteFile(workspace, halt))
    registry.register(CreateFolder(workspace, halt))
    policy = Policy(tmp_path)
    policy.enabled[Permission.FOLDER] = True
    executor = SkillExecutor(registry, policy, halt, AuditLog(tmp_path / "audit.jsonl"))
    return executor, halt, policy


def test_create_folder_makes_one_level_and_rejects_collisions(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    executor, halt, policy = _executor(tmp_path, workspace)

    def fail_recursive(*args, **kwargs):
        raise AssertionError("recursive folder creation")

    monkeypatch.setattr("jarvis.skills.files.os.makedirs", fail_recursive)
    policy.enabled[Permission.FOLDER] = False
    disabled = executor.submit("create_folder", {"path": "notes/child"})
    assert disabled.outcome == "disabled"
    assert not (workspace / "notes" / "child").exists()
    policy.enabled[Permission.FOLDER] = True

    pending = executor.submit("create_folder", {"path": "notes/child"})
    assert pending.outcome == "needs_approval"
    created = executor.confirm(pending.action_id, approved=True)
    assert created.ok is True
    assert created.text == "Created notes/child."
    assert (workspace / "notes" / "child").is_dir()
    assert str(tmp_path) not in created.text

    existing = executor.confirm(executor.submit("create_folder", {"path": "notes"}).action_id, approved=True)
    assert existing.error_type == "already_exists"
    assert (workspace / "notes").is_dir()
    file_hit = executor.confirm(executor.submit("create_folder", {"path": "note.txt"}).action_id, approved=True)
    assert file_hit.error_type == "already_file"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "original note"
    missing = executor.confirm(executor.submit("create_folder", {"path": "missing/child"}).action_id, approved=True)
    assert missing.error_type == "not_found"
    assert not (workspace / "missing").exists()
    deeper = executor.confirm(executor.submit("create_folder", {"path": "notes/absent/grand"}).action_id, approved=True)
    assert deeper.error_type == "not_found"
    assert not (workspace / "notes" / "absent").exists()

    raced = executor.submit("create_folder", {"path": "notes/race"})
    (workspace / "notes" / "race").mkdir()
    (workspace / "notes" / "race" / "keep.txt").write_text("keep", encoding="utf-8")
    blocked = executor.confirm(raced.action_id, approved=True)
    assert blocked.error_type == "already_exists"
    assert (workspace / "notes" / "race" / "keep.txt").read_text(encoding="utf-8") == "keep"

    other = executor.submit("create_folder", {"path": "notes/denied"})
    denied = executor.confirm(other.action_id, approved=False)
    assert denied.outcome == "denied"
    assert not (workspace / "notes" / "denied").exists()


def test_create_folder_rejects_escape_and_stop(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill = CreateFolder(workspace, HaltController())
    outside = tmp_path / "outside"
    outside.mkdir()
    for raw in ("../outside/new", str(outside / "new"), "notes/../../outside/new", "/tmp/new", "CON"):
        result = skill.execute({"path": raw})
        assert result.ok is False
        assert not (outside / "new").exists()
        assert str(outside) not in result.text
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    escaped = skill.execute({"path": "escape/owned"})
    assert escaped.error_type == "outside_workspace"
    assert not (outside / "owned").exists()
    named = skill.execute({"path": "escape"})
    assert named.error_type == "outside_workspace"
    assert outside.is_dir()

    executor, halt, policy = _executor(tmp_path, workspace)
    waiting = executor.submit("create_folder", {"path": "notes/pending"})
    halt.emergency_stop()
    direct = executor._registry.get("create_folder").execute({"path": "notes/pending"})
    assert direct.outcome == "cancelled"
    assert direct.text == ""
    assert not (workspace / "notes" / "pending").exists()
    halt.resume()
    replayed = executor.confirm(waiting.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert not (workspace / "notes" / "pending").exists()


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


def test_create_folder_api_keeps_other_file_skills(tmp_path: Path):
    workspace = _workspace(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Enable Create Folder" in page
        assert "Request creation" in page
        shown = page.split("function showFolderResult", 1)[1].split("async function decideFolder", 1)[0]
        assert "textContent" in shown
        assert "innerHTML" not in shown
        renderer = page.split("async function loadFolderSkill", 1)[1].split("function showFolderResult", 1)[0]
        assert "textContent" in renderer
        assert "innerHTML" not in renderer

        off = client.post("/api/skills/folder", json={"path": "notes/child"}).json()
        assert off["outcome"] == "disabled"
        assert not (workspace / "notes" / "child").exists()
        empty = client.post("/api/skills/folder", json={"path": "  "})
        assert empty.status_code == 400
        enabled = client.post("/api/skills/folder/permission", json={"enabled": True}).json()
        assert enabled == {"permission": "folder", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["folder"] is True
        assert permissions["read"] is False
        assert permissions["list"] is False
        assert permissions["search"] is False
        assert permissions["write"] is False

        escaped = client.post("/api/skills/folder", json={"path": "../secret"}).json()
        assert escaped["outcome"] == "rejected"
        assert not (tmp_path / "secret").exists()
        collision = client.post("/api/skills/folder", json={"path": "notes"}).json()
        assert collision["error_type"] == "already_exists"
        occupied = client.post("/api/skills/folder", json={"path": "note.txt"}).json()
        assert occupied["error_type"] == "already_file"
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "original note"
        absent = client.post("/api/skills/folder", json={"path": "missing/child"}).json()
        assert absent["error_type"] == "not_found"
        assert not (workspace / "missing").exists()
        requested = client.post("/api/skills/folder", json={"path": "notes/child"}).json()
        assert requested["outcome"] == "needs_approval"
        assert requested["path"] == "notes/child"
        crossed = client.post("/api/skills/write/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        assert crossed["outcome"] == "rejected"
        assert not (workspace / "notes" / "child").exists()
        opened = client.post("/api/skills/folder/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        assert opened["text"] == "Created notes/child."
        assert (workspace / "notes" / "child").is_dir()

        client.post("/api/skills/read/permission", json={"enabled": True})
        client.post("/api/skills/list/permission", json={"enabled": True})
        client.post("/api/skills/search/permission", json={"enabled": True})
        client.post("/api/skills/write/permission", json={"enabled": True})
        read_request = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        listed = client.post("/api/skills/list", json={"path": "notes"}).json()
        found = client.post("/api/skills/search", json={"query": "child"}).json()
        write = client.post("/api/skills/write", json={"path": "notes/child/page.txt", "content": "kept"}).json()
        read = client.post("/api/skills/read/confirm", json={"action_id": read_request["action_id"], "approved": True}).json()
        listing = client.post("/api/skills/list/confirm", json={"action_id": listed["action_id"], "approved": True}).json()
        search = client.post("/api/skills/search/confirm", json={"action_id": found["action_id"], "approved": True}).json()
        saved = client.post("/api/skills/write/confirm", json={"action_id": write["action_id"], "approved": True}).json()
        assert read["text"] == "original note"
        assert "child/" in listing["text"]
        assert "notes/child/" in search["text"] or "notes/child" in search["text"]
        assert saved["text"] == "Created notes/child/page.txt."
        assert (workspace / "notes" / "child" / "page.txt").read_text(encoding="utf-8") == "kept"

        live = client.post("/api/skills/folder", json={"path": "notes/later"}).json()
        client.post("/api/emergency-stop")
        cancelled = client.post("/api/skills/folder/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert cancelled["outcome"] == "cancelled"
        assert cancelled["text"] == ""
        assert not (workspace / "notes" / "later").exists()
        assert client.post("/api/resume").json()["replayed"] is False
        assert client.get("/api/skills/folder").json()["pending"] == []
        replayed = client.post("/api/skills/folder/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert not (workspace / "notes" / "later").exists()
