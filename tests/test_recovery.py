"""Phase 9.3 Agent Reliability & Recovery — mocked safety tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.agent.goal import _MAX_READONLY_RETRIES
from jarvis.config import Settings
from jarvis.integrations.protect import MemoryProtector
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
        # Tests inject a non-DPAPI protector so recovery suites run on non-Windows CI hosts.
        integration_protector=MemoryProtector(),
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


def _enable_multi(client: TestClient) -> None:
    assert client.post("/api/workflow/multi-app/permission", json={"enabled": True}).status_code == 200


def _enable_system(client: TestClient) -> None:
    assert client.post("/api/skills/system/permission", json={"enabled": True}).status_code == 200


def _enable_browser_workflow(client: TestClient, app) -> tuple[str, str]:
    assert client.post("/api/browser/bridge/permission", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/api/browser/bridge/actions/permission", json={"enabled": True}).json()["enabled"] is True
    assert client.post("/api/browser/bridge/workflow/permission", json={"enabled": True}).json()["enabled"] is True
    token = app.state.browser_bridge._bridge_token
    pair = client.get("/api/browser/bridge").json()["pair_code"]
    return token, pair


def _browser_handshake(client: TestClient, token: str, pair: str) -> str:
    reply = client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {
                "type": "handshake",
                "pair_code": pair,
                "browser": "Google Chrome",
                "extension_version": "1.2.0",
                "protocol_version": "1.2.0",
            },
            "raw_size": 120,
        },
    ).json()
    assert reply["ok"] is True
    return reply["session_id"]


def test_startup_seals_multi_app_without_auto_resume(tmp_path: Path):
    """Interrupted multi-app checkpoints need review; engines stay idle."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        _enable_multi(client)
        proposed = client.post(
            "/api/workflow/multi-app/propose",
            json={"steps": [{"capability": "system_info"}]},
        ).json()
        plan_id = proposed["run"]["plan_id"]
        assert proposed["run"]["status"] == "proposed"
        active = client.get("/api/recovery").json()["active"]
        assert any(row["execution_id"] == plan_id and row["kind"] == "multi_app" for row in active)

    app2 = _app(tmp_path)
    with TestClient(app2) as client:
        recovery = client.get("/api/recovery").json()
        interrupted = recovery["interrupted"]
        assert recovery["active"] == []
        assert any(row["execution_id"] == plan_id and row["recovery_status"] == "interrupted" for row in interrupted)
        live = client.get("/api/workflow/multi-app").json()
        run = live.get("run") or {}
        assert run.get("plan_id") in {"", None} or run.get("status") in {"", "idle", None, "cancelled"}
        assert run.get("plan_id") != plan_id or run.get("status") != "proposed"


def test_startup_seals_browser_workflow_without_auto_resume(tmp_path: Path):
    """Browser workflow active rows seal on startup and never auto-start."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_browser_workflow(client, app)
        _browser_handshake(client, token, pair)
        proposed = client.post(
            "/api/browser/bridge/workflow/propose",
            json={"steps": [{"action": "observe"}]},
        ).json()
        plan_id = proposed["run"]["plan_id"]
        assert proposed["run"]["status"] == "proposed"
        assert any(
            row["execution_id"] == plan_id and row["kind"] == "browser_workflow"
            for row in client.get("/api/recovery").json()["active"]
        )

    app2 = _app(tmp_path)
    with TestClient(app2) as client:
        recovery = client.get("/api/recovery").json()
        assert recovery["active"] == []
        assert any(
            row["execution_id"] == plan_id and row["recovery_status"] == "interrupted"
            for row in recovery["interrupted"]
        )
        workflow = client.get("/api/browser/bridge/workflow").json()
        run = workflow.get("run") or {}
        assert run.get("plan_id") != plan_id or run.get("status") not in {"proposed", "awaiting_approval", "running"}


def test_startup_seals_multiple_kinds_without_resuming_any(tmp_path: Path):
    """Goal + multi-app active checkpoints all seal; none auto-resume."""
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        _enable_multi(client)
        goal = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        multi = client.post(
            "/api/workflow/multi-app/propose",
            json={"steps": [{"capability": "system_info"}]},
        ).json()
        goal_id = goal["plan_id"]
        multi_id = multi["run"]["plan_id"]
        active_ids = {row["execution_id"] for row in client.get("/api/recovery").json()["active"]}
        assert {goal_id, multi_id} <= active_ids

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        recovery = client.get("/api/recovery").json()
        interrupted_ids = {row["execution_id"] for row in recovery["interrupted"]}
        assert recovery["active"] == []
        assert {goal_id, multi_id} <= interrupted_ids
        assert client.get("/api/agent/goal").json().get("status") != "awaiting_approval"
        multi_live = (client.get("/api/workflow/multi-app").json().get("run") or {})
        assert multi_live.get("status") != "awaiting_approval"


def test_active_checkpoint_cannot_restart_mid_run(tmp_path: Path):
    """Restart is only for interrupted/needs_review — never while still active."""
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        plan_id = proposed["plan_id"]
        row = next(item for item in client.get("/api/recovery").json()["active"] if item["execution_id"] == plan_id)
        refused = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert refused["ok"] is False
        assert client.get("/api/agent/goal").json()["plan_id"] == plan_id
        assert client.get("/api/agent/goal").json()["status"] == "proposed"


def test_abandon_interrupted_run_clears_recovery_and_live_goal(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        plan_id = proposed["plan_id"]
        started = client.post("/api/agent/goal/start", json={"plan_id": plan_id, "approved": True}).json()
        old_action = started["current"]["action_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        row = client.get("/api/recovery").json()["interrupted"][0]
        abandoned = client.post("/api/recovery/abandon", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert abandoned["ok"] is True
        leftover = client.get("/api/recovery").json()
        assert all(item["execution_id"] != plan_id for item in leftover["interrupted"] + leftover["active"])
        assert client.get("/api/agent/goal").json().get("plan_id") != plan_id or client.get("/api/agent/goal").json().get(
            "status"
        ) in {"cancelled", "idle", "", None}
        # Stale approval from the abandoned run must never succeed.
        refused = client.post("/api/agent/goal/confirm", json={"action_id": old_action, "approved": True}).json()
        assert refused.get("ok") is False or refused.get("status") in {"rejected", "cancelled", "idle", "", None}


def test_restart_refused_while_emergency_stopped(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        plan_id = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()["plan_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        _enable_list(client)
        row = next(item for item in client.get("/api/recovery").json()["interrupted"] if item["execution_id"] == plan_id)
        assert client.post("/api/emergency-stop").status_code == 200
        assert client.get("/api/status").json()["status"] == "stopped"
        refused = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert refused["ok"] is False
        # Still interrupted — stop must not auto-abandon or auto-resume.
        assert any(item["checkpoint_id"] == row["checkpoint_id"] for item in client.get("/api/recovery").json()["interrupted"])
        assert client.get("/api/agent/goal").json().get("status") != "proposed" or client.get("/api/agent/goal").json().get(
            "plan_id"
        ) in {"", None, plan_id}


def test_resume_after_stop_does_not_auto_resume_interrupted(tmp_path: Path):
    """Resume clears halt only — interrupted checkpoints stay for explicit recovery."""
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        plan_id = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()["plan_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        interrupted_before = client.get("/api/recovery").json()["interrupted"]
        assert any(row["execution_id"] == plan_id for row in interrupted_before)
        client.post("/api/emergency-stop")
        client.post("/api/resume")
        assert client.get("/api/status").json()["status"] != "stopped"
        recovery = client.get("/api/recovery").json()
        assert any(row["execution_id"] == plan_id and row["recovery_status"] == "interrupted" for row in recovery["interrupted"])
        goal = client.get("/api/agent/goal").json()
        assert goal.get("status") != "awaiting_approval"
        assert goal.get("plan_id") != plan_id or goal.get("status") not in {"proposed", "awaiting_approval", "running"}


def test_restart_from_beginning_after_mid_step_progress(tmp_path: Path, monkeypatch):
    """Even after a completed step, recovery restart proposes a brand-new plan at the start."""
    fake = FakeProvider()
    fake.goal_reply = LIST_THEN_WRITE
    app = _app(tmp_path, fake)

    def list_ok(self, arguments):  # noqa: ANN001
        return SkillResult(True, text="listed", outcome="completed")

    monkeypatch.setattr("jarvis.skills.files.ListFiles.execute", list_ok)
    with TestClient(app) as client:
        _enable_list(client)
        _enable_write(client)
        proposed = client.post("/api/agent/goal", json={"task": "List then write"}).json()
        plan_id = proposed["plan_id"]
        started = client.post("/api/agent/goal/start", json={"plan_id": plan_id, "approved": True}).json()
        after_list = client.post(
            "/api/agent/goal/confirm",
            json={"action_id": started["current"]["action_id"], "approved": True},
        ).json()
        assert after_list["status"] == "awaiting_approval"
        assert after_list["current"]["skill"] == "write_file"
        assert after_list["steps"][0]["state"] == "completed"
        old_write_action = after_list["current"]["action_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        _enable_list(client)
        _enable_write(client)
        row = next(item for item in client.get("/api/recovery").json()["interrupted"] if item["execution_id"] == plan_id)
        assert row["step_index"] >= 1
        restarted = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert restarted["ok"] is True
        goal = client.get("/api/agent/goal").json()
        assert goal["status"] == "proposed"
        assert goal["plan_id"] != plan_id
        assert all(step["state"] != "completed" for step in goal["steps"])
        # Mid-step approval from the interrupted run must not apply to the new plan.
        refused = client.post("/api/agent/goal/confirm", json={"action_id": old_write_action, "approved": True}).json()
        assert refused.get("ok") is False or refused.get("status") in {"rejected", "proposed"}
        assert client.get("/api/agent/goal").json()["status"] == "proposed"


def test_restart_refuses_empty_or_secret_payload(tmp_path: Path):
    store = CheckpointStore(tmp_path / "data" / "workflow_checkpoints.sqlite3")
    empty_id = "1" * 32
    secret_id = "2" * 32
    store.upsert(
        {
            "checkpoint_id": empty_id,
            "execution_id": "3" * 32,
            "kind": "goal",
            "status": "interrupted",
            "recovery_status": "interrupted",
            "step_index": 0,
            "step_count": 1,
            "approval_pending": False,
            "summary": "Goal: empty payload",
            "restart_payload": {},
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    store.upsert(
        {
            "checkpoint_id": secret_id,
            "execution_id": "4" * 32,
            "kind": "goal",
            "status": "interrupted",
            "recovery_status": "interrupted",
            "step_index": 0,
            "step_count": 1,
            "approval_pending": False,
            "summary": "Goal: secret task",
            "restart_payload": {"task": "use sk-abcdefghijklmnopqrstuvwxyz123456"},
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        empty = client.post("/api/recovery/restart", json={"checkpoint_id": empty_id}).json()
        assert empty["ok"] is False
        secret = client.post("/api/recovery/restart", json={"checkpoint_id": secret_id}).json()
        assert secret["ok"] is False
        # Fail closed: no new goal proposed from unsafe payloads.
        goal = client.get("/api/agent/goal").json()
        assert goal.get("status") in {"", "idle", None, "cancelled"} or goal.get("plan_id") in {"", None}


def test_corrupt_step_bounds_and_invalid_ids_fail_closed(tmp_path: Path):
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
            "f" * 32,
            "a" * 32,
            "goal",
            "interrupted",
            "interrupted",
            99,
            1,
            0,
            "bad bounds",
            json.dumps({"task": "List the workspace"}),
            "2020-01-01T00:00:00Z",
            "2020-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    with TestClient(app) as client:
        state = client.get("/api/recovery").json()
        assert all(row["checkpoint_id"] != "f" * 32 for row in state["interrupted"] + state["active"])
        assert client.post("/api/recovery/abandon", json={"checkpoint_id": "f" * 32}).json()["ok"] is False
        assert client.post("/api/recovery/restart", json={"checkpoint_id": "f" * 32}).json()["ok"] is False
        # Request-model validation rejects non-32-char ids before recovery runs.
        assert client.post("/api/recovery/abandon", json={"checkpoint_id": "not-a-valid-id"}).status_code == 422
        assert client.post("/api/recovery/restart", json={"checkpoint_id": "zzz"}).status_code == 422
        # 32-char but non-hex / unknown ids fail closed at the service layer.
        unknown = "z" * 32
        assert client.post("/api/recovery/abandon", json={"checkpoint_id": unknown}).json()["ok"] is False
        assert client.post("/api/recovery/restart", json={"checkpoint_id": unknown}).json()["ok"] is False


def test_public_recovery_status_never_leaks_payload_or_approvals(tmp_path: Path):
    store = CheckpointStore(tmp_path / "data" / "workflow_checkpoints.sqlite3")
    store.upsert(
        {
            "checkpoint_id": "9" * 32,
            "execution_id": "8" * 32,
            "kind": "goal",
            "status": "interrupted",
            "recovery_status": "interrupted",
            "step_index": 0,
            "step_count": 1,
            "approval_pending": True,
            "summary": "Goal: List the workspace",
            "restart_payload": {
                "task": "List the workspace",
                "approval_id": "leak-me",
                "action_id": "also-leak",
            },
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    app = _app(tmp_path)
    with TestClient(app) as client:
        raw = json.dumps(client.get("/api/recovery").json())
        assert "leak-me" not in raw
        assert "also-leak" not in raw
        assert "restart_payload" not in raw
        assert "approval_id" not in raw
        row = client.get("/api/recovery").json()["interrupted"][0]
        assert row["can_restart"] is True
        assert "restart_payload" not in row


def test_terminal_completed_checkpoint_is_not_offered_for_resume(tmp_path: Path, monkeypatch):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)

    def list_ok(self, arguments):  # noqa: ANN001
        return SkillResult(True, text="listed", outcome="completed")

    monkeypatch.setattr("jarvis.skills.files.ListFiles.execute", list_ok)
    with TestClient(app) as client:
        _enable_list(client)
        proposed = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()
        plan_id = proposed["plan_id"]
        started = client.post("/api/agent/goal/start", json={"plan_id": plan_id, "approved": True}).json()
        done = client.post(
            "/api/agent/goal/confirm",
            json={"action_id": started["current"]["action_id"], "approved": True},
        ).json()
        assert done["status"] == "completed"
        recovery = client.get("/api/recovery").json()
        assert all(row["execution_id"] != plan_id for row in recovery["interrupted"] + recovery["active"])

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        recovery = client.get("/api/recovery").json()
        assert all(row["execution_id"] != plan_id for row in recovery["interrupted"] + recovery["active"])
        assert client.get("/api/agent/goal").json().get("status") != "awaiting_approval"


def test_multi_app_restart_creates_new_execution_without_old_approvals(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _enable_multi(client)
        _enable_system(client)
        skill = app.state.skills.get("system_info")
        skill._reader = lambda _halt: "Operating system: Windows\nMemory: test"
        proposed = client.post(
            "/api/workflow/multi-app/propose",
            json={"steps": [{"capability": "system_info"}]},
        ).json()
        plan_id = proposed["run"]["plan_id"]
        started = client.post(
            "/api/workflow/multi-app/start",
            json={"plan_id": plan_id, "approved": True},
        ).json()
        old_action = started["pending"]["action_id"]
        assert old_action

    app2 = _app(tmp_path)
    with TestClient(app2) as client:
        _enable_multi(client)
        _enable_system(client)
        skill = app2.state.skills.get("system_info")
        skill._reader = lambda _halt: "Operating system: Windows\nMemory: test"
        row = next(
            item
            for item in client.get("/api/recovery").json()["interrupted"]
            if item["execution_id"] == plan_id and item["kind"] == "multi_app"
        )
        restarted = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert restarted["ok"] is True
        live = client.get("/api/workflow/multi-app").json()
        new_run = live.get("run") or {}
        assert new_run.get("status") == "proposed"
        assert new_run.get("plan_id") != plan_id
        # Old nested approval must not complete the new run.
        refused = client.post(
            "/api/workflow/multi-app/confirm",
            json={"action_id": old_action, "approved": True},
        ).json()
        run_after = refused.get("run") or client.get("/api/workflow/multi-app").json().get("run") or {}
        assert run_after.get("status") == "proposed"
        assert run_after.get("plan_id") == new_run.get("plan_id")


def test_browser_workflow_restart_from_beginning_only(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_browser_workflow(client, app)
        session_id = _browser_handshake(client, token, pair)
        # Observe arming requires a fresh (possibly empty) target scan — no live browser.
        scanned = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={
                "message": {
                    "type": "action_targets",
                    "session_id": session_id,
                    "browser": "Google Chrome",
                    "tab_title": "Example Docs",
                    "tab_url": "https://example.com/docs",
                    "elements": [],
                },
                "raw_size": 200,
            },
        ).json()
        assert scanned.get("ok") is True
        proposed = client.post(
            "/api/browser/bridge/workflow/propose",
            json={"steps": [{"action": "observe"}, {"action": "observe"}]},
        ).json()
        plan_id = proposed["run"]["plan_id"]
        started = client.post(
            "/api/browser/bridge/workflow/start",
            json={"plan_id": plan_id, "approved": True},
        ).json()
        old_action = (started.get("pending") or {}).get("action_id") or (
            client.get("/api/browser/bridge/workflow").json().get("pending") or {}
        ).get("action_id")
        assert old_action
        assert started["run"]["status"] in {"waiting", "awaiting_approval", "running"} or started.get("pending")

    app2 = _app(tmp_path)
    with TestClient(app2) as client:
        token, pair = _enable_browser_workflow(client, app2)
        _browser_handshake(client, token, pair)
        row = next(
            item
            for item in client.get("/api/recovery").json()["interrupted"]
            if item["execution_id"] == plan_id and item["kind"] == "browser_workflow"
        )
        assert row["can_restart"] is True
        restarted = client.post("/api/recovery/restart", json={"checkpoint_id": row["checkpoint_id"]}).json()
        assert restarted["ok"] is True
        workflow = client.get("/api/browser/bridge/workflow").json()
        new_run = workflow.get("run") or {}
        assert new_run.get("status") == "proposed"
        assert new_run.get("plan_id") != plan_id
        assert (new_run.get("index") or 0) == 0
        refused = client.post(
            "/api/browser/bridge/workflow/confirm",
            json={"action_id": old_action, "approved": True},
        ).json()
        after = refused.get("run") or client.get("/api/browser/bridge/workflow").json().get("run") or {}
        assert after.get("status") == "proposed"
        assert after.get("plan_id") == new_run.get("plan_id")


def test_abandoned_checkpoint_cannot_be_restarted(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        plan_id = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()["plan_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        _enable_list(client)
        row = next(item for item in client.get("/api/recovery").json()["interrupted"] if item["execution_id"] == plan_id)
        checkpoint_id = row["checkpoint_id"]
        assert client.post("/api/recovery/abandon", json={"checkpoint_id": checkpoint_id}).json()["ok"] is True
        again = client.post("/api/recovery/restart", json={"checkpoint_id": checkpoint_id}).json()
        assert again["ok"] is False
        assert all(item["checkpoint_id"] != checkpoint_id for item in client.get("/api/recovery").json()["interrupted"])


def test_concurrent_restart_attempts_create_at_most_one_live_plan(tmp_path: Path):
    fake = FakeProvider()
    fake.goal_reply = LIST_ONLY
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        _enable_list(client)
        plan_id = client.post("/api/agent/goal", json={"task": "List the workspace"}).json()["plan_id"]

    app2 = _app(tmp_path, fake)
    with TestClient(app2) as client:
        _enable_list(client)
        row = next(item for item in client.get("/api/recovery").json()["interrupted"] if item["execution_id"] == plan_id)
        checkpoint_id = row["checkpoint_id"]
        results: list[dict] = []
        errors: list[BaseException] = []

        def restart() -> None:
            try:
                results.append(client.post("/api/recovery/restart", json={"checkpoint_id": checkpoint_id}).json())
            except BaseException as exc:  # noqa: BLE001 — collect race outcomes
                errors.append(exc)

        threads = [threading.Thread(target=restart) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert any(item.get("ok") for item in results)
        assert sum(1 for item in results if item.get("ok")) >= 1
        # Checkpoint is gone from interrupted; at most one live proposed goal remains.
        leftover = client.get("/api/recovery").json()
        assert all(item["checkpoint_id"] != checkpoint_id for item in leftover["interrupted"])
        goal = client.get("/api/agent/goal").json()
        assert goal["status"] == "proposed"
        assert goal["plan_id"] != plan_id
        refused = client.post("/api/recovery/restart", json={"checkpoint_id": checkpoint_id}).json()
        assert refused["ok"] is False


def test_seal_interrupted_on_startup_never_calls_executors(tmp_path: Path):
    """Unit: sealing active rows must not propose/start any workflow engine."""
    from jarvis.audit import AuditLog
    from jarvis.safety.halt import HaltController

    class BoomGoal:
        def propose(self, *_a, **_k):
            raise AssertionError("startup must not propose a goal")

        def cancel(self):
            raise AssertionError("startup must not cancel via seal")

        def snapshot(self):
            return {}

    class BoomMulti:
        def propose(self, *_a, **_k):
            raise AssertionError("startup must not propose multi-app")

        def cancel(self):
            raise AssertionError("startup must not cancel multi-app via seal")

        def status(self):
            return {"run": {}}

    store = CheckpointStore(tmp_path / "cp.sqlite3")
    store.upsert(
        {
            "checkpoint_id": "7" * 32,
            "execution_id": "6" * 32,
            "kind": "goal",
            "status": "proposed",
            "recovery_status": "active",
            "step_index": 0,
            "step_count": 1,
            "approval_pending": False,
            "summary": "Goal: List the workspace",
            "restart_payload": {"task": "List the workspace"},
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    halt = HaltController()
    audit = AuditLog(tmp_path / "audit.jsonl")
    service = RecoveryService(
        halt,
        audit,
        db_path=tmp_path / "cp.sqlite3",
        goal_agent=BoomGoal(),
        multi_app=BoomMulti(),
    )
    status = service.status()
    assert status["active"] == []
    assert status["interrupted"][0]["execution_id"] == "6" * 32
    assert status["interrupted"][0]["recovery_status"] == "interrupted"
