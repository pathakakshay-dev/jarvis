from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.orchestrator import SUPPORTED_SKILLS, PlanError
from jarvis.skills.planner import parse_proposed_plan
from jarvis.skills.registry import SkillRegistry
from jarvis.skills.system import SystemInfo, _Stopped, collect_system_report


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


def _executor(tmp_path: Path, reader):
    halt = HaltController()
    calls = []

    def wrapped(active):
        calls.append(active.halted)
        return reader(active)

    registry = SkillRegistry()
    registry.register(SystemInfo(halt, reader=wrapped))
    policy = Policy(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return SkillExecutor(registry, policy, halt, audit), policy, halt, calls


def test_system_permission_stays_off_until_enabled(tmp_path: Path):
    executor, policy, _halt, calls = _executor(tmp_path, lambda _halt: "Operating system: Windows")
    refused = executor.submit("system_info", {})
    assert refused.outcome == "disabled"
    assert refused.text == "That skill is not enabled."
    assert calls == []
    policy.enabled[Permission.SYSTEM] = True
    waiting = executor.submit("system_info", {})
    assert waiting.outcome == "needs_approval"
    assert calls == []
    denied = executor.confirm(waiting.action_id, approved=False)
    assert denied.outcome == "denied"
    assert calls == []


def test_approval_runs_the_reader_once(tmp_path: Path):
    executor, policy, _halt, calls = _executor(tmp_path, lambda _halt: "Operating system: Windows\nMemory: 1.0 GB available of 2.0 GB")
    policy.enabled[Permission.SYSTEM] = True
    waiting = executor.submit("system_info", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.outcome == "completed"
    assert result.text.startswith("Operating system: Windows")
    assert calls == [False]
    replay = executor.confirm(waiting.action_id, approved=True)
    assert replay.error_type == "unknown_action"
    assert calls == [False]
    extra = executor.submit("system_info", {"command": "powershell"})
    assert extra.error_type == "invalid_arguments"
    assert calls == [False]


def test_emergency_stop_discards_the_report_and_resume_does_not_replay(tmp_path: Path):
    def reader(active):
        active.emergency_stop()
        return "Operating system: SECRET-REPORT"

    executor, policy, halt, calls = _executor(tmp_path, reader)
    policy.enabled[Permission.SYSTEM] = True
    waiting = executor.submit("system_info", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.outcome == "cancelled"
    assert result.text == ""
    assert "SECRET-REPORT" not in (result.text or "")
    assert calls == [False]
    halted = executor.submit("system_info", {})
    assert halted.outcome == "cancelled"
    assert calls == [False]
    halt.resume()
    replay = executor.confirm(waiting.action_id, approved=True)
    assert replay.outcome == "rejected"
    assert replay.text != "Operating system: SECRET-REPORT"
    assert calls == [False]


def test_reader_failure_does_not_expose_the_exception(tmp_path: Path):
    def reader(_active):
        raise RuntimeError(r"C:\Users\secret\ntuser.dat")

    executor, policy, _halt, calls = _executor(tmp_path, reader)
    policy.enabled[Permission.SYSTEM] = True
    waiting = executor.submit("system_info", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.outcome == "rejected"
    assert result.text == "System information is unavailable."
    assert "secret" not in result.text
    assert "ntuser" not in result.text
    assert calls == [False]


def test_stopped_reader_returns_no_text(tmp_path: Path):
    def reader(active):
        active.emergency_stop()
        raise _Stopped()

    skill = SystemInfo(HaltController(), reader=reader)
    result = skill.execute({})
    assert result.outcome == "cancelled"
    assert result.text == ""


def test_system_api_is_separate_from_file_permissions(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), fake=FakeProvider(reply="local reply"), start_hotkeys_flag=False)
    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.json()["permissions"]["system"] is False
        page = client.get("/")
        assert 'id="system-result"' in page.text
        assert "System information is off." in page.text
        refused = client.post("/api/skills/system", json={})
        assert refused.status_code == 200
        assert refused.json()["outcome"] == "disabled"
        assert "Windows" not in refused.json()["text"]
        injected = client.post("/api/skills/system", json={"command": "powershell"})
        assert injected.status_code == 422
        enabled = client.post("/api/skills/system/permission", json={"enabled": True})
        assert enabled.json() == {"permission": "system", "enabled": True}
        body = client.get("/api/status").json()["permissions"]
        assert body["system"] is True
        assert body["read"] is False
        assert body["write"] is False
        assert body["folder"] is False
        waiting = client.post("/api/skills/system", json={})
        action_id = waiting.json()["action_id"]
        stolen = client.post("/api/skills/read/confirm", json={"action_id": action_id, "approved": True})
        assert stolen.json()["error_type"] == "unknown_action"
        denied = client.post("/api/skills/system/confirm", json={"action_id": action_id, "approved": False})
        assert denied.json()["outcome"] == "denied"
        again = client.post("/api/skills/system", json={})
        action_id = again.json()["action_id"]
        client.post("/api/emergency-stop")
        client.post("/api/resume")
        replay = client.post("/api/skills/system/confirm", json={"action_id": action_id, "approved": True})
        assert replay.json()["outcome"] == "cancelled"
        assert replay.json()["text"] == ""
        fresh = client.post("/api/skills/system", json={})
        approved = client.post(
            "/api/skills/system/confirm",
            json={"action_id": fresh.json()["action_id"], "approved": True},
        )
        report = approved.json()
        assert report["outcome"] == "completed"
        assert "Operating system:" in report["text"]
        assert "Memory:" in report["text"]
        assert "System drive:" in report["text"]
        assert "Battery:" in report["text"]
        assert "Uptime:" in report["text"]
        assert "\\" not in report["text"]
        assert "password" not in report["text"].lower()
        second = client.post("/api/skills/system/confirm", json={"action_id": fresh.json()["action_id"], "approved": True})
        assert second.json()["error_type"] == "unknown_action"


def test_planner_cannot_request_system_information():
    assert "system_info" not in SUPPORTED_SKILLS
    try:
        parse_proposed_plan('{"steps":[{"skill":"system_info","arguments":{}}]}')
    except PlanError as exc:
        assert "unsupported" in str(exc).lower()
    else:
        raise AssertionError("system information was accepted as a plan step")


def test_live_report_uses_windows_summary_only():
    report = collect_system_report(HaltController())
    for label in ("Operating system:", "CPU:", "Logical processors:", "CPU usage:", "Memory:", "System drive:", "Battery:", "Uptime:"):
        assert label in report
    assert "\\" not in report
    assert "Users" not in report
    lowered = report.lower()
    assert "password" not in lowered
    assert "powershell" not in lowered


def test_system_skill_does_not_run_commands():
    source = Path("jarvis/skills/system.py").read_text(encoding="utf-8").lower()
    for blocked in ("subprocess", "powershell", "os.system", "cmd.exe", "shell=true"):
        assert blocked not in source
