import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.skills.files import MAX_FILE_BYTES


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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
    )


def _app(tmp_path: Path):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(reply="local reply"),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
    )


def test_files_section_uses_approval_for_one_read(tmp_path: Path):
    app = _app(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "note.txt").write_text("hello workspace", encoding="utf-8")
    (workspace / "other.md").write_text("<script>alert(1)</script>\n", encoding="utf-8")
    (workspace / "code.py").write_text("print(1)", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Enable Read File" in page
        assert "Request read" in page
        assert 'id="file-result"' in page
        shown = page.split("function showFileResult", 1)[1].split("async function decideFile", 1)[0]
        assert "textContent" in shown
        assert "innerHTML" not in shown
        assert "speakText" not in page.split("async function decideFile", 1)[1].split("async function setReadPermission", 1)[0]

        refused = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        assert refused["outcome"] == "disabled"
        assert "hello workspace" not in refused["text"]
        assert client.get("/api/skills/read").json()["pending"] == []

        enabled = client.post("/api/skills/read/permission", json={"enabled": True}).json()
        assert enabled == {"permission": "read", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["read"] is True
        assert permissions["write"] is False

        first = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        second = client.post("/api/skills/read", json={"path": "other.md"}).json()
        assert first["outcome"] == "needs_approval"
        assert second["outcome"] == "needs_approval"
        assert first["text"] == "That action needs approval."
        pending = client.get("/api/skills/read").json()["pending"]
        assert {row["path"] for row in pending} == {"note.txt", "other.md"}
        assert {row["skill"] for row in pending} == {"read_file"}

        denied = client.post("/api/skills/read/confirm", json={"action_id": second["action_id"], "approved": False}).json()
        assert denied["outcome"] == "denied"
        assert "alert(1)" not in denied["text"]

        opened = client.post("/api/skills/read/confirm", json={"action_id": first["action_id"], "approved": True}).json()
        assert opened["ok"] is True
        assert opened["text"] == "hello workspace"
        assert opened["path"] == "note.txt"
        assert str(tmp_path) not in json.dumps(opened)

        leaked = client.post("/api/skills/read/confirm", json={"action_id": second["action_id"], "approved": True}).json()
        assert leaked["ok"] is False
        assert "alert(1)" not in leaked["text"]

        escaped = client.post("/api/skills/read", json={"path": "../secret.txt"}).json()
        blocked = client.post("/api/skills/read/confirm", json={"action_id": escaped["action_id"], "approved": True}).json()
        assert blocked["error_type"] == "outside_workspace"
        assert blocked["text"] == "That file is outside the workspace."
        assert "secret" not in blocked["text"]
        assert str(secret) not in json.dumps(blocked)

        source = client.post("/api/skills/read", json={"path": "code.py"}).json()
        rejected = client.post("/api/skills/read/confirm", json={"action_id": source["action_id"], "approved": True}).json()
        assert rejected["error_type"] == "unsupported_type"
        assert "print(1)" not in rejected["text"]

        client.post("/api/skills/read/permission", json={"enabled": True})
        held = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        assert held["outcome"] == "needs_approval"
        client.post("/api/skills/read/permission", json={"enabled": False})
        blocked_permission = client.post(
            "/api/skills/read/confirm",
            json={"action_id": held["action_id"], "approved": True},
        ).json()
        assert blocked_permission["outcome"] == "disabled"
        assert "hello workspace" not in blocked_permission["text"]

        client.post("/api/skills/read/permission", json={"enabled": True})
        live = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        client.post("/api/emergency-stop")
        cancelled = client.post("/api/skills/read/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert cancelled["outcome"] == "cancelled"
        assert cancelled["text"] == ""
        resumed = client.post("/api/resume").json()
        assert resumed["replayed"] is False
        assert client.get("/api/skills/read").json()["pending"] == []
        replayed = client.post("/api/skills/read/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert "hello workspace" not in replayed["text"]

        chat = client.post("/api/chat", json={"text": "Hello"}).json()
        assert chat["status"] == "completed"
        assert chat["text"] == "local reply"


def test_stopped_list_drops_pending_reads_without_replaying(tmp_path: Path):
    app = _app(tmp_path)
    (tmp_path / "workspace" / "note.txt").write_text("hello workspace", encoding="utf-8")
    with TestClient(app) as client:
        client.post("/api/skills/read/permission", json={"enabled": True})
        live = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        client.post("/api/emergency-stop")
        assert client.get("/api/skills/read").json()["pending"] == []
        client.post("/api/resume")
        replayed = client.post("/api/skills/read/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert "hello workspace" not in replayed["text"]


def test_read_api_rejects_a_symlink_and_a_large_file(tmp_path: Path):
    app = _app(tmp_path)
    workspace = tmp_path / "workspace"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is not available")
    (workspace / "huge.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with TestClient(app) as client:
        client.post("/api/skills/read/permission", json={"enabled": True})
        linked = client.post("/api/skills/read", json={"path": "link.txt"}).json()
        escaped = client.post("/api/skills/read/confirm", json={"action_id": linked["action_id"], "approved": True}).json()
        assert escaped["error_type"] == "outside_workspace"
        assert "secret" not in escaped["text"]
        huge = client.post("/api/skills/read", json={"path": "huge.txt"}).json()
        too_big = client.post("/api/skills/read/confirm", json={"action_id": huge["action_id"], "approved": True}).json()
        assert too_big["error_type"] == "too_large"
        assert "xxx" not in too_big["text"]
