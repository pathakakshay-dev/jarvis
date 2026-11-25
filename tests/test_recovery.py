"""Phase 9.3 Agent Reliability & Recovery — mocked safety tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.agent.goal import _MAX_READONLY_RETRIES
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.recovery.service import RecoveryService
from jarvis.recovery.store import CheckpointStore
from jarvis.skills.base import SkillResult


LIST_ONLY = json.dumps({"steps": [{"skill": "list_files", "arguments": {"path": ""}}]})
LIST_THEN_WRITE = json.dumps(
    {
        "steps": [
            {"skill": "list_files", "arguments": {"path": ""}},
            {"skill": "write_file", "arguments": {"path": "notes.txt", "content": "hello"}},
        ]
    }
)
WRITE_ONLY = json.dumps(
    {"steps": [{"skill": "write_file", "arguments": {"path": "notes.txt", "content": "hello"}}]}
)


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


def _app(tmp_path: Path, fake: FakeProvider | None = None):
    return create_app(
        settings=_settings(tmp_path),
        fake=fake or FakeProvider(reply="local reply"),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: True,
        calculator_launcher=lambda *_a, **_k: True,
        start_reminder_scheduler=False,
    )


def _enable_list(client: TestClient) -> None:
    assert client.post("/api/skills/list/permission", json={"enabled": True}).status_code == 200


def _enable_write(client: TestClient) -> None:
    assert client.post("/api/skills/write/permission", json={"enabled": True}).status_code == 200


def test_interrupted_workflow_needs_review_after_restart(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        assert proposed["status"] == "proposed"
        plan_id = proposed["plan_id"]
        state = client.get("/api/recovery").json()
        assert any(row["execution_id"] == plan_id and row["recovery_status"] == "active" for row in state["active"])
        assert client.get("/api/agent/goal").json()["status"] == "proposed"

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        recovery = client.get("/api/recovery").json()
        interrupted = recovery["interrupted"]
        assert interrupted
        assert interrupted[0]["recovery_status"] == "interrupted"
        assert interrupted[0]["execution_id"] == plan_id
        assert recovery["active"] == []
        goal = client.get("/api/agent/goal").json()
        assert goal.get("status") in {"", "idle", None} or goal.get("plan_id") in {"", None}
        assert "proposed" != goal.get("status") or goal.get("plan_id") != plan_id


def test_corrupted_checkpoint_fail_closed(tmp_path: Path):
    app = _app(tmp_path)
    db = tmp_path / "data" / "workflow_checkpoints.sqlite3"
    with TestClient(app) as client:
        assert client.get("/api/recovery").json()["ok"] is True
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO workflow_checkpoints (
            checkpoint_id, execution_id, kind, status, recovery_status,
            step_index, step_count, approval_pending, summary, restart_payload,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "a" * 32,
            "b" * 32,
            "evil_kind",
            "running",
            "interrupted",
            0,
            1,
            0,
            "bad",
            "{not-json",
            "2020-01-01T00:00:00Z",
            "2020-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    with TestClient(app) as client:
        state = client.get("/api/recovery").json()
        assert all(row["checkpoint_id"] != "a" * 32 for row in state["interrupted"])
        refused = client.post("/api/recovery/abandon", json={"checkpoint_id": "a" * 32}).json()
        assert refused["ok"] is False
        restart = client.post("/api/recovery/restart", json={"checkpoint_id": "a" * 32}).json()
        assert restart["ok"] is False


def test_duplicate_execution_and_concurrent_recovery(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        first = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        second = client.post("/api/agent/goal", json={"task": "List again"}).json()
        assert second.get("ok") is False or "already" in (second.get("text") or "").lower()
        plan_id = first["plan_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        row = client.get("/api/recovery").json()["interrupted"][0]
        assert row["execution_id"] == plan_id
        checkpoint_id = row["checkpoint_id"]
        results: list[dict] = []
        errors: list[BaseException] = []

        def abandon() -> None:
            try:
                results.append(client.post("/api/recovery/abandon", json={"checkpoint_id": checkpoint_id}).json())
            except BaseException as exc:  # noqa: BLE001 — collect race outcomes
                errors.append(exc)

        threads = [threading.Thread(target=abandon) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert any(item.get("ok") for item in results)
        leftover = client.get("/api/recovery").json()
        assert all(item["checkpoint_id"] != checkpoint_id for item in leftover["interrupted"])
        again = client.post("/api/recovery/abandon", json={"checkpoint_id": checkpoint_id}).json()
        assert again["ok"] is False


def test_stop_cancellation_persisted(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        plan_id = proposed["plan_id"]
        started = client.post("/api/agent/goal/start", json={"plan_id": plan_id, "approved": True}).json()
        assert started["status"] == "awaiting_approval"
        client.post("/api/emergency-stop")
        assert client.get("/api/status").json()["status"] == "stopped"
        recovery = client.get("/api/recovery").json()
        rows = recovery["interrupted"] + recovery["active"]
        # Stop seals the execution as cancelled; it must not remain active for auto-resume.
        assert all(row["execution_id"] != plan_id for row in rows)
        goal = client.get("/api/agent/goal").json()
        assert goal["status"] == "cancelled"


def test_approval_invalidation_on_restart_begin(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        plan_id = proposed["plan_id"]
        started = client.post("/api/agent/goal/start", json={"plan_id": plan_id, "approved": True}).json()
        old_action = started["current"]["action_id"]
        assert old_action

    # Seal as interrupted without auto-resume.
    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        _enable_list(client)
        row = client.get("/api/recovery").json()["interrupted"][0]
        assert row["execution_id"] == plan_id
        assert row["can_restart"] is True
        restarted = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert restarted["ok"] is True
        assert restarted["restart"]["ok"] is True
        goal = client.get("/api/agent/goal").json()
        assert goal["status"] == "proposed"
        assert goal["plan_id"] != plan_id
        # Old approval id must not work on the new execution.
        refused = client.post("/api/agent/goal/confirm", json={"action_id": old_action, "approved": True}).json()
        assert refused.get("ok") is False or refused.get("status") in {"rejected", "proposed"}
        assert goal["plan_id"] == client.get("/api/agent/goal").json()["plan_id"]
        # Restarted checkpoint is gone; approvals were not reused.
        leftover = client.get("/api/recovery").json()
        assert all(item["execution_id"] != plan_id for item in leftover["interrupted"])


def test_readonly_retry_bounded_and_requires_reapproval(tmp_path: Path, monkeypatch):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    calls = {"n": 0}

    def flaky(self, arguments):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= _MAX_READONLY_RETRIES:
            return SkillResult(False, text="timed out", outcome="error", error_type="timeout")
        return SkillResult(True, text="ok", outcome="completed")

    monkeypatch.setattr("jarvis.skills.files.ListFiles.execute", flaky)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        started = client.post(
            "/api/agent/goal/start", json={"plan_id": proposed["plan_id"], "approved": True}
        ).json()
        first_action = started["current"]["action_id"]
        retry1 = client.post("/api/agent/goal/confirm", json={"action_id": first_action, "approved": True}).json()
        assert retry1["status"] == "awaiting_approval"
        assert retry1["current"]["action_id"] != first_action
        second_action = retry1["current"]["action_id"]
        retry2 = client.post("/api/agent/goal/confirm", json={"action_id": second_action, "approved": True}).json()
        assert retry2["status"] == "awaiting_approval"
        third_action = retry2["current"]["action_id"]
        done = client.post("/api/agent/goal/confirm", json={"action_id": third_action, "approved": True}).json()
        assert done["status"] == "completed"
        assert calls["n"] == _MAX_READONLY_RETRIES + 1


def test_write_actions_are_never_auto_retried(tmp_path: Path, monkeypatch):
    fake = FakeProvider()
    fake.goal_reply = WRITE_ONLY
    app = _app(tmp_path, fake)
    calls = {"n": 0}

    def boom(self, arguments):  # noqa: ANN001
        calls["n"] += 1
        return SkillResult(False, text="timed out", outcome="error", error_type="timeout")

    monkeypatch.setattr("jarvis.skills.files.WriteFile.execute", boom)
    with TestClient(app) as client:
        _enable_write(client)
        proposed = client.post("/api/agent/goal", json={"task": "Write notes.txt"}).json()
        started = client.post(
            "/api/agent/goal/start", json={"plan_id": proposed["plan_id"], "approved": True}
        ).json()
        failed = client.post(
            "/api/agent/goal/confirm",
            json={"action_id": started["current"]["action_id"], "approved": True},
        ).json()
        assert failed["status"] == "failed"
        assert calls["n"] == 1
        assert client.get("/api/agent/goal").json()["status"] == "failed"


def test_partial_failure_does_not_replay_completed_steps(tmp_path: Path, monkeypatch):
    fake = FakeProvider()
    fake.goal_reply = LIST_THEN_WRITE
    app = _app(tmp_path, fake)
    list_calls = {"n": 0}

    def list_once(self, arguments):  # noqa: ANN001
        list_calls["n"] += 1
        return SkillResult(True, text="listed", outcome="completed")

    def write_fail(self, arguments):  # noqa: ANN001
        return SkillResult(False, text="write failed", outcome="error", error_type="execution_error")

    monkeypatch.setattr("jarvis.skills.files.ListFiles.execute", list_once)
    monkeypatch.setattr("jarvis.skills.files.WriteFile.execute", write_fail)
    with TestClient(app) as client:
        _enable_list(client)
        _enable_write(client)
        proposed = client.post("/api/agent/goal", json={"task": "List then write"}).json()
        started = client.post(
            "/api/agent/goal/start", json={"plan_id": proposed["plan_id"], "approved": True}
        ).json()
        after_list = client.post(
            "/api/agent/goal/confirm",
            json={"action_id": started["current"]["action_id"], "approved": True},
        ).json()
        assert after_list["status"] == "awaiting_approval"
        failed = client.post(
            "/api/agent/goal/confirm",
            json={"action_id": after_list["current"]["action_id"], "approved": True},
        ).json()
        assert failed["status"] == "failed"
        assert list_calls["n"] == 1


def test_restart_from_beginning_never_reuses_approvals(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    store = CheckpointStore(tmp_path / "data" / "workflow_checkpoints.sqlite3")
    # Seed an interrupted checkpoint with a fabricated approval id that must never appear in restart.
    store.upsert(
        {
            "checkpoint_id": "c" * 32,
            "execution_id": "d" * 32,
            "kind": "goal",
            "status": "interrupted",
            "recovery_status": "interrupted",
            "step_index": 1,
            "step_count": 2,
            "approval_pending": True,
            "summary": "Goal: List the workspace",
            "restart_payload": {"task": "List the workspace", "approval_id": "should-not-persist"},
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        state = client.get("/api/recovery").json()
        assert state["interrupted"][0]["checkpoint_id"] == "c" * 32
        assert "approval_id" not in json.dumps(state)
        body = client.post("/api/recovery/restart", json={"checkpoint_id": "c" * 32}).json()
        assert body["ok"] is True
        goal = client.get("/api/agent/goal").json()
        assert goal["status"] == "proposed"
        assert goal["plan_id"] != "d" * 32
        assert goal.get("action_id") in {"", None}


def test_recovery_service_rejects_unknown_state_unit(tmp_path: Path):
    from jarvis.audit import AuditLog
    from jarvis.safety.halt import HaltController

    halt = HaltController()
    audit = AuditLog(tmp_path / "audit.jsonl")
    service = RecoveryService(halt, audit, db_path=tmp_path / "cp.sqlite3")
    service.record_goal(
        {
            "plan_id": "not-hex",
            "status": "running",
            "index": 0,
            "steps": [],
            "task": "x",
        }
    )
    assert service.status()["interrupted"] == []
    assert service.status()["active"] == []
    assert service.abandon("e" * 32)["ok"] is False
