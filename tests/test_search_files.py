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
from jarvis.skills.files import (
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_VISITS,
    ListFiles,
    ReadFile,
    SearchFiles,
)
from jarvis.skills.registry import SkillRegistry


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    notes = workspace / "notes"
    inner = notes / "inner"
    inner.mkdir(parents=True)
    (workspace / "note.txt").write_text("hello workspace", encoding="utf-8")
    (notes / "sample.txt").write_text("hidden contents", encoding="utf-8")
    (inner / "deep.txt").write_text("deep contents", encoding="utf-8")
    return workspace


def _executor(tmp_path: Path, workspace: Path):
    halt = HaltController()
    registry = SkillRegistry()
    registry.register(ReadFile(workspace, halt))
    registry.register(ListFiles(workspace, halt))
    registry.register(SearchFiles(workspace, halt))
    policy = Policy(tmp_path)
    policy.enabled[Permission.SEARCH] = True
    executor = SkillExecutor(registry, policy, halt, AuditLog(tmp_path / "audit.jsonl"))
    return executor, halt, policy


def _approve(executor: SkillExecutor, query: str):
    pending = executor.submit("search_files", {"query": query})
    assert pending.outcome == "needs_approval"
    return executor.confirm(pending.action_id, approved=True)


def test_search_matches_filenames_only(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    executor, halt, policy = _executor(tmp_path, workspace)
    policy.enabled[Permission.SEARCH] = False
    disabled = executor.submit("search_files", {"query": "sample"})
    assert disabled.outcome == "disabled"
    assert "sample.txt" not in disabled.text
    assert "hidden contents" not in disabled.text
    policy.enabled[Permission.SEARCH] = True
    found = _approve(executor, "SaMpLe")
    assert found.ok is True
    assert found.text == "notes/sample.txt"
    assert "hidden contents" not in found.text
    assert "deep.txt" not in found.text
    assert str(tmp_path) not in found.text
    missing = _approve(executor, "not-in-the-workspace")
    assert missing.ok is True
    assert missing.text == "(no matches)"

    skill = SearchFiles(workspace, HaltController())

    def fail_open(*args, **kwargs):
        raise AssertionError("search opened a file")

    monkeypatch.setattr(Path, "open", fail_open)
    monkeypatch.setattr(Path, "read_text", fail_open)
    monkeypatch.setattr(Path, "read_bytes", fail_open)
    direct = skill.execute({"query": "sample"})
    assert direct.text == "notes/sample.txt"


def test_search_rejects_paths_and_symlink_escapes(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill = SearchFiles(workspace, HaltController())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET-BODY", encoding="utf-8")
    for raw in ("../outside", "notes/../../outside", str(outside), "notes\\secret.txt", "/etc/passwd", "..", "a/b"):
        result = skill.execute({"query": raw})
        assert result.ok is False
        assert result.error_type == "invalid_query"
        assert "SECRET-BODY" not in result.text
        assert str(outside) not in result.text
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    escaped = skill.execute({"query": "secret"})
    assert escaped.ok is True
    assert "secret.txt" not in escaped.text
    assert "SECRET-BODY" not in escaped.text
    assert str(outside) not in escaped.text
    named = skill.execute({"query": "escape"})
    assert str(outside) not in named.text
    assert "SECRET-BODY" not in named.text


def test_search_limits_depth_results_and_scan_errors(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    deep = workspace
    for part in ("a", "b", "c", "d", "e"):
        deep = deep / part
    deep.mkdir(parents=True)
    (workspace / "found.txt").write_text("near body", encoding="utf-8")
    (deep / "hidden.txt").write_text("far body", encoding="utf-8")
    skill = SearchFiles(workspace, HaltController())
    limited = skill.execute({"query": "txt"})
    assert "found.txt" in limited.text
    assert "hidden.txt" not in limited.text
    assert "far body" not in limited.text
    assert "More items were not shown." in limited.text

    crowded = tmp_path / "crowded"
    crowded.mkdir()
    for index in range(MAX_SEARCH_RESULTS + 5):
        (crowded / f"hit-{index:02d}.txt").write_text("BODY", encoding="utf-8")
    capped = SearchFiles(crowded, HaltController()).execute({"query": "hit"})
    lines = [line for line in capped.text.splitlines() if line.startswith("hit-")]
    assert lines == [f"hit-{index:02d}.txt" for index in range(MAX_SEARCH_RESULTS)]
    assert "More items were not shown." in capped.text
    assert "BODY" not in capped.text

    wide = tmp_path / "wide"
    wide.mkdir()
    for index in range(MAX_SEARCH_VISITS):
        (wide / f"a{index:03d}.txt").write_text("bulk", encoding="utf-8")
    (wide / "zzz-target.txt").write_text("late", encoding="utf-8")
    scanned = SearchFiles(wide, HaltController()).execute({"query": "zzz"})
    assert "zzz-target.txt" not in scanned.text
    assert "late" not in scanned.text
    assert scanned.text == "More items were not shown."

    def fail_scan(path):
        raise OSError("private disk detail")

    monkeypatch.setattr("jarvis.skills.files.os.scandir", fail_scan)
    failed = skill.execute({"query": "found"})
    assert failed.error_type == "search_error"
    assert failed.text == "That folder could not be searched."
    assert "private" not in failed.text


def test_search_approval_is_one_query_and_stop_does_not_replay(tmp_path: Path):
    workspace = _workspace(tmp_path)
    executor, halt, policy = _executor(tmp_path, workspace)
    sample = executor.submit("search_files", {"query": "sample"})
    other = executor.submit("search_files", {"query": "deep"})
    denied = executor.confirm(other.action_id, approved=False)
    assert denied.outcome == "denied"
    assert "deep.txt" not in denied.text
    opened = executor.confirm(sample.action_id, approved=True)
    assert opened.text == "notes/sample.txt"
    assert "deep.txt" not in opened.text
    again = executor.confirm(other.action_id, approved=True)
    assert again.ok is False
    assert "deep.txt" not in again.text

    waiting = executor.submit("search_files", {"query": "sample"})
    halt.emergency_stop()
    direct = executor._registry.get("search_files").execute({"query": "sample"})
    assert direct.outcome == "cancelled"
    assert direct.text == ""
    halt.resume()
    replayed = executor.confirm(waiting.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert "sample.txt" not in replayed.text


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


def test_search_api_keeps_read_and_list_unchanged(tmp_path: Path):
    workspace = _workspace(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Enable Search Files" in page
        assert "Request search" in page
        shown = page.split("function showSearchResult", 1)[1].split("async function decideSearch", 1)[0]
        assert "textContent" in shown
        assert "innerHTML" not in shown

        empty = client.post("/api/skills/search", json={"query": "  "})
        assert empty.status_code == 400
        off = client.post("/api/skills/search", json={"query": "sample"}).json()
        assert off["outcome"] == "disabled"
        assert "sample.txt" not in off["text"]
        enabled = client.post("/api/skills/search/permission", json={"enabled": True}).json()
        assert enabled == {"permission": "search", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["search"] is True
        assert permissions["read"] is False
        assert permissions["list"] is False

        requested = client.post("/api/skills/search", json={"query": "sample"}).json()
        assert requested["outcome"] == "needs_approval"
        assert requested["path"] == "sample"
        pending = client.get("/api/skills/search").json()["pending"]
        assert pending[0]["skill"] == "search_files"
        assert pending[0]["query"] == "sample"
        crossed_read = client.post("/api/skills/read/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        crossed_list = client.post("/api/skills/list/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        assert crossed_read["outcome"] == "rejected"
        assert crossed_list["outcome"] == "rejected"
        assert "sample.txt" not in crossed_read["text"]
        assert "hidden contents" not in crossed_list["text"]
        opened = client.post("/api/skills/search/confirm", json={"action_id": requested["action_id"], "approved": True}).json()
        assert opened["text"] == "notes/sample.txt"
        assert "hidden contents" not in opened["text"]
        assert str(tmp_path) not in json.dumps(opened)
        assert (workspace / "notes" / "sample.txt").read_text(encoding="utf-8") == "hidden contents"

        other = client.post("/api/skills/search", json={"query": "deep"}).json()
        denied = client.post("/api/skills/search/confirm", json={"action_id": other["action_id"], "approved": False}).json()
        assert denied["outcome"] == "denied"
        assert "deep.txt" not in denied["text"]

        client.post("/api/skills/read/permission", json={"enabled": True})
        client.post("/api/skills/list/permission", json={"enabled": True})
        read_request = client.post("/api/skills/read", json={"path": "note.txt"}).json()
        listed = client.post("/api/skills/list", json={"path": "notes"}).json()
        blocked = client.post("/api/skills/search/confirm", json={"action_id": read_request["action_id"], "approved": True}).json()
        assert blocked["outcome"] == "rejected"
        assert "hello workspace" not in blocked["text"]
        read = client.post("/api/skills/read/confirm", json={"action_id": read_request["action_id"], "approved": True}).json()
        listing = client.post("/api/skills/list/confirm", json={"action_id": listed["action_id"], "approved": True}).json()
        assert read["text"] == "hello workspace"
        assert listing["text"].splitlines() == ["inner/", "sample.txt"]
        assert "deep contents" not in listing["text"]

        live = client.post("/api/skills/search", json={"query": "sample"}).json()
        client.post("/api/emergency-stop")
        cancelled = client.post("/api/skills/search/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert cancelled["outcome"] == "cancelled"
        assert cancelled["text"] == ""
        assert client.post("/api/resume").json()["replayed"] is False
        assert client.get("/api/skills/search").json()["pending"] == []
        replayed = client.post("/api/skills/search/confirm", json={"action_id": live["action_id"], "approved": True}).json()
        assert replayed["ok"] is False
        assert "sample.txt" not in replayed["text"]
