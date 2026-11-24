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
from jarvis.skills.files import MAX_FILE_BYTES, ListFiles, ReadFile, SearchFiles, WriteFile
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
    policy = Policy(tmp_path)
    policy.enabled[Permission.WRITE] = True
    executor = SkillExecutor(registry, policy, halt, AuditLog(tmp_path / "audit.jsonl"))
    return executor, halt, policy


def _approve(executor: SkillExecutor, path: str, content: str, mode: str):
    pending = executor.submit("write_file", {"path": path, "content": content, "mode": mode})
    assert pending.outcome == "needs_approval"
    return pending, executor.confirm(pending.action_id, approved=True)


def test_write_creates_and_requires_a_separate_overwrite(tmp_path: Path):
    workspace = _workspace(tmp_path)
    executor, halt, policy = _executor(tmp_path, workspace)
    policy.enabled[Permission.WRITE] = False
    disabled = executor.submit("write_file", {"path": "notes/page.txt", "content": "new", "mode": "create"})
    assert disabled.outcome == "disabled"
    assert not (workspace / "notes" / "page.txt").exists()
    policy.enabled[Permission.WRITE] = True

    pending, created = _approve(executor, "notes/page.txt", "hello write", "create")
    assert created.ok is True
    assert created.text == "Created notes/page.txt."
    assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "hello write"
    assert str(tmp_path) not in created.text

    queued = executor.submit("write_file", {"path": "notes/page.txt", "content": "replaced", "mode": "overwrite"})
    assert queued.outcome == "needs_approval"
    shown = executor.pending()
    assert shown[0]["mode"] == "overwrite"
    assert shown[0]["path"] == "notes/page.txt"
    assert shown[0]["content"] == "replaced"
    denied = executor.confirm(queued.action_id, approved=False)
    assert denied.outcome == "denied"
    assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "hello write"

    raced = executor.submit("write_file", {"path": "notes/fresh.txt", "content": "late", "mode": "create"})
    (workspace / "notes" / "fresh.txt").write_text("kept", encoding="utf-8")
    blocked = executor.confirm(raced.action_id, approved=True)
    assert blocked.error_type == "already_exists"
    assert (workspace / "notes" / "fresh.txt").read_text(encoding="utf-8") == "kept"

    updated = executor.confirm(
        executor.submit("write_file", {"path": "notes/page.txt", "content": "replaced", "mode": "overwrite"}).action_id,
        approved=True,
    )
    assert updated.text == "Updated notes/page.txt."
    assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "replaced"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "original note"


def test_write_rejects_escape_type_and_size(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill = WriteFile(workspace, HaltController())
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SECRET-BODY", encoding="utf-8")
    for raw in ("../outside/secret.txt", str(secret), "notes\\..\\secret.txt", "/etc/passwd.txt", "notes/file.py"):
        before = secret.read_text(encoding="utf-8")
        result = skill.execute({"path": raw, "content": "pwned", "mode": "create"})
        assert result.ok is False
        assert "pwned" not in result.text
        assert secret.read_text(encoding="utf-8") == before
        assert not (outside / "passwd.txt").exists()
    assert skill.execute({"path": "notes/file.py", "content": "print(1)", "mode": "create"}).error_type == "unsupported_type"
    assert not (workspace / "notes" / "file.py").exists()
    assert skill.execute({"path": "missing/page.txt", "content": "x", "mode": "create"}).error_type == "not_found"
    assert not (workspace / "missing").exists()

    exact = skill.execute({"path": "notes/full.txt", "content": "a" * MAX_FILE_BYTES, "mode": "create"})
    assert exact.ok is True
    assert (workspace / "notes" / "full.txt").read_bytes() == b"a" * MAX_FILE_BYTES
    oversized = skill.execute({"path": "notes/big.txt", "content": "é" * 60_000, "mode": "create"})
    assert oversized.error_type == "too_large"
    assert not (workspace / "notes" / "big.txt").exists()
    nulled = skill.execute({"path": "notes/null.txt", "content": "a\x00b", "mode": "create"})
    assert nulled.error_type == "invalid_content"
    assert not (workspace / "notes" / "null.txt").exists()

    link = workspace / "link.txt"
    folder = workspace / "escape"
    try:
        link.symlink_to(secret)
        folder.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    through_file = skill.execute({"path": "link.txt", "content": "pwned", "mode": "overwrite"})
    through_dir = skill.execute({"path": "escape/owned.txt", "content": "pwned", "mode": "create"})
    assert through_file.error_type == "outside_workspace"
    assert through_dir.error_type == "outside_workspace"
    assert secret.read_text(encoding="utf-8") == "SECRET-BODY"
    assert not (outside / "owned.txt").exists()


def test_write_stop_discards_pending_content(tmp_path: Path):
    workspace = _workspace(tmp_path)
    executor, halt, policy = _executor(tmp_path, workspace)
    waiting = executor.submit("write_file", {"path": "notes/pending.txt", "content": "do not write", "mode": "create"})
    assert waiting.outcome == "needs_approval"
    halt.emergency_stop()
    direct = executor._registry.get("write_file").execute(
        {"path": "notes/pending.txt", "content": "do not write", "mode": "create"}
    )
    assert direct.outcome == "cancelled"
    assert direct.text == ""
    assert not (workspace / "notes" / "pending.txt").exists()
    halt.resume()
    replayed = executor.confirm(waiting.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert not (workspace / "notes" / "pending.txt").exists()
    assert executor.pending() == []


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


def test_write_api_keeps_read_list_and_search(tmp_path: Path):
    workspace = _workspace(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Enable Write File" in page
        assert "Request write" in page
        assert "This will overwrite the existing file." in page
        shown = page.split("function showWriteResult", 1)[1].split("async function decideWrite", 1)[0]
        assert "textContent" in shown
        assert "innerHTML" not in shown
        renderer = page.split("async function loadWriteSkill", 1)[1].split("function showWriteResult", 1)[0]
        assert "textContent" in renderer
        assert "innerHTML" not in renderer

        off = client.post("/api/skills/write", json={"path": "notes/page.txt", "content": "new"}).json()
        assert off["outcome"] == "disabled"
        assert "mode" not in off
        assert not (workspace / "notes" / "page.txt").exists()
        empty = client.post("/api/skills/write", json={"path": "  ", "content": "new"})
        assert empty.status_code == 400
        huge = client.post("/api/skills/write", json={"path": "notes/page.txt", "content": "a" * (MAX_FILE_BYTES + 1)})
        assert huge.status_code == 422
        assert not (workspace / "notes" / "page.txt").exists()

        enabled = client.post("/api/skills/write/permission", json={"enabled": True}).json()
        assert enabled == {"permission": "write", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["write"] is True
        assert permissions["read"] is False
        assert permissions["list"] is False
        assert permissions["search"] is False

        escaped = client.post("/api/skills/write", json={"path": "../secret.txt", "content": "pwned"}).json()
        assert escaped["outcome"] == "rejected"
        assert not (tmp_path / "secret.txt").exists()
        typed = client.post("/api/skills/write", json={"path": "notes/file.py", "content": "print(1)"}).json()
        assert typed["error_type"] == "unsupported_type"
        assert not (workspace / "notes" / "file.py").exists()

        created = client.post("/api/skills/write", json={"path": "notes/page.txt", "content": "<script>alert(1)</script>"}).json()
        assert created["outcome"] == "needs_approval"
        assert created["mode"] == "create"
        pending = client.get("/api/skills/write").json()["pending"]
        assert pending[0]["mode"] == "create"
        assert pending[0]["content"] == "<script>alert(1)</script>"
        crossed = client.post("/api/skills/read/confirm", json={"action_id": created["action_id"], "approved": True}).json()
        assert crossed["outcome"] == "rejected"
        assert not (workspace / "notes" / "page.txt").exists()
        opened = client.post("/api/skills/write/confirm", json={"action_id": created["action_id"], "approved": True}).json()
        assert opened["text"] == "Created notes/page.txt."
        assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "<script>alert(1)</script>"

        other = client.post("/api/skills/write", json={"path": "notes/page.md", "content": "second"}).json()
        denied = client.post("/api/skills/write/confirm", json={"action_id": other["action_id"], "approved": False}).json()
        assert denied["outcome"] == "denied"
        assert not (workspace / "notes" / "page.md").exists()

        replace = client.post("/api/skills/write", json={"path": "notes/page.txt", "content": "safe text"}).json()
        assert replace["mode"] == "overwrite"
        waiting = client.get("/api/skills/write").json()["pending"][0]
        assert waiting["mode"] == "overwrite"
        assert "overwrite" in json.dumps(waiting)
        client.post("/api/skills/write/permission", json={"enabled": False})
        blocked = client.post("/api/skills/write/confirm", json={"action_id": replace["action_id"], "approved": True}).json()
        assert blocked["outcome"] == "disabled"
        assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "<script>alert(1)</script>"
        client.post("/api/skills/write/permission", json={"enabled": True})
        saved = client.post("/api/skills/write/confirm", json={"action_id": replace["action_id"], "approved": True}).json()
        assert saved["text"] == "Updated notes/page.txt."
        assert (workspace / "notes" / "page.txt").read_text(encoding="utf-8") == "safe text"

        client.post("/api/skills/read/permission", json={"enabled": True})
        client.post("/api/skills/list/permission", json={"enabled": True})
        client.post("/api/skills/search/permission", json={"enabled": True})
        read_request = client.post("/api/skills/read", json={"path": "notes/page.txt"}).json()
        listed = client.post("/api/skills/list", json={"path": "notes"}).json()
        found = client.post("/api/skills/search", json={"query": "page"}).json()
        read = client.post("/api/skills/read/confirm", json={"action_id": read_request["action_id"], "approved": True}).json()
        listing = client.post("/api/skills/list/confirm", json={"action_id": listed["action_id"], "approved": True}).json()
        search = client.post("/api/skills/search/confirm", json={"action_id": found["action_id"], "approved": True}).json()
        assert read["text"] == "safe text"
        assert "page.txt" in listing["text"]
        assert "safe text" not in listing["text"]
        assert "notes/page.txt" in search["text"]
        assert "safe text" not in search["text"]

        live = client.post("/api/skills/write", json={"path": "notes/later.txt", "content": "do not write"}).json()
        client.post("/api/emergency-stop")
        cancelled = client.post("/api/skills/write/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert cancelled["outcome"] == "cancelled"
        assert cancelled["text"] == ""
        assert not (workspace / "notes" / "later.txt").exists()
        assert client.post("/api/resume").json()["replayed"] is False
        assert client.get("/api/skills/write").json()["pending"] == []
        replayed = client.post("/api/skills/write/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert not (workspace / "notes" / "later.txt").exists()
