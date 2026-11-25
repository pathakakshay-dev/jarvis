"""Open Notepad only after a fresh approval. These tests never start a real process."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as RAW_TEST_CLIENT

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.desktop.notepad import NotepadOpener, launch_notepad, notepad_launch_path
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.orchestrator import SUPPORTED_SKILLS
from jarvis.skills.planner import _FIELDS


@pytest.fixture(autouse=True)
def _block_real_notepad_process(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("tests must not start Notepad")

    monkeypatch.setattr("jarvis.desktop.notepad._create_process", blocked)


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


class _Launcher:
    def __init__(self) -> None:
        self.generations: list[int] = []
        self.paths: list[str] = []

    def launch(self, generation: int) -> bool:
        self.generations.append(generation)
        return True

    def start(self, path: str) -> bool:
        self.paths.append(path)
        return True


def _app(tmp_path: Path, launcher: _Launcher):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=launcher.launch,
    )


def _opener(tmp_path: Path, launcher: _Launcher, *, clock=None, ttl_s: float = 120):
    halt = HaltController()
    policy = Policy(tmp_path)
    opener = NotepadOpener(
        halt,
        AuditLog(tmp_path / "audit.jsonl"),
        policy,
        launcher=launcher.launch,
        clock=clock,
        ttl_s=ttl_s,
    )
    return halt, policy, opener


def test_notepad_launch_accepts_only_the_system_binary():
    assert notepad_launch_path(r"C:\Windows\System32") == r"C:\Windows\System32\notepad.exe"
    assert notepad_launch_path(r"C:\Windows\SysWOW64") == r"C:\Windows\SysWOW64\notepad.exe"
    rejected = [
        r"C:\Windows\System32\cmd.exe",
        r"C:\Windows\System32\notepad.exe",
        r"C:\Users\a\notepad.exe",
        r"C:\Windows\System32\..\System32",
        "https://evil.example/notepad.exe",
        r"C:\Windows\System32\notepad.exe /c calc",
        "notepad.exe",
        "",
        None,
    ]
    for raw in rejected:
        assert notepad_launch_path(raw) is None
    launcher = _Launcher()
    assert launch_notepad(system_directory=r"C:\Windows\System32", starter=launcher.start, allow=lambda: True) is True
    assert launcher.paths == [r"C:\Windows\System32\notepad.exe"]
    for raw in rejected:
        assert launch_notepad(system_directory=raw, starter=launcher.start, allow=lambda: True) is False
    assert launcher.paths == [r"C:\Windows\System32\notepad.exe"]
    assert launch_notepad(system_directory=r"C:\Windows\System32", starter=launcher.start, allow=lambda: False) is False
    assert launcher.paths == [r"C:\Windows\System32\notepad.exe"]


def test_supplied_directory_cannot_replace_the_system_binary(monkeypatch):
    created: list[str] = []

    def fake_create(path: str, allow=None) -> bool:
        if allow is not None and not allow():
            return False
        created.append(path)
        return True

    monkeypatch.setattr("jarvis.desktop.notepad.read_system_directory", lambda: r"C:\Windows\System32")
    monkeypatch.setattr("jarvis.desktop.notepad._create_process", fake_create)
    assert launch_notepad(system_directory=r"C:\evil\System32", allow=lambda: True) is True
    assert launch_notepad(system_directory=r"C:\Windows\System32\cmd.exe", allow=lambda: True) is True
    assert created == [r"C:\Windows\System32\notepad.exe", r"C:\Windows\System32\notepad.exe"]


def test_request_does_not_launch_and_approval_launches_once(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        state = client.get("/api/desktop/notepad")
        assert state.status_code == 200
        assert state.json()["permission_enabled"] is False
        assert state.json()["action"] == "Open Notepad"
        assert state.json()["application"] == "Notepad"
        assert state.json()["launched"] is False
        blocked = client.post("/api/desktop/notepad", json={})
        assert blocked.status_code == 200
        assert blocked.json()["launched"] is False
        assert launcher.generations == []
        enabled = client.post("/api/desktop/notepad/permission", json={"enabled": True})
        assert enabled.json() == {"permission": "app_control", "enabled": True}
        assert app.state.policy.enabled[Permission.APPS] is False
        assert app.state.policy.enabled[Permission.READ] is False
        requested = client.post("/api/desktop/notepad", json={})
        body = requested.json()
        assert body["launched"] is False
        assert body["pending"]["action"] == "Open Notepad"
        assert body["pending"]["application"] == "Notepad"
        assert launcher.generations == []
        action_id = body["pending"]["action_id"]
        approved = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert approved.json()["launched"] is True
        assert approved.json()["detail"] == "Notepad was opened."
        assert len(launcher.generations) == 1
        again = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert again.json()["launched"] is False
        assert len(launcher.generations) == 1
        assert EXECUTION_ENABLED is False
        assert "/api/desktop/notepad/execute" not in {route.path for route in app.routes if hasattr(route, "path")}


def test_arbitrary_names_paths_and_parameters_are_rejected(tmp_path: Path):
    launcher = _Launcher()
    halt, _policy, opener = _opener(tmp_path, launcher)
    del halt
    opener_result = opener.request({"application": "cmd.exe", "path": r"C:\Windows\System32\cmd.exe", "args": "/c calc"})
    assert opener_result["launched"] is False
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        names = client.post("/api/desktop/notepad", json={"application": "calc.exe"})
        paths = client.post(
            "/api/desktop/notepad",
            json={"path": r"C:\Users\a\notepad.exe", "url": "https://evil.example", "args": "/c calc"},
        )
        confirm = client.post(
            "/api/desktop/notepad/confirm",
            json={"action_id": "a" * 32, "approved": True, "application": "cmd.exe", "path": r"C:\Windows\System32\cmd.exe"},
        )
        permission = client.post("/api/desktop/notepad/permission", json={"enabled": True, "path": r"C:\evil\app.exe"})
        assert names.status_code == 422
        assert paths.status_code == 422
        assert confirm.status_code == 422
        assert permission.status_code == 422
        assert app.state.policy.enabled[Permission.APP_CONTROL] is True
        assert launcher.generations == []
        audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "cmd.exe" not in audit
        assert "evil.example" not in audit


def test_permission_disabled_blocks_execution(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        assert app.state.policy.enabled[Permission.APP_CONTROL] is False
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        requested = client.post("/api/desktop/notepad", json={})
        action_id = requested.json()["pending"]["action_id"]
        client.post("/api/desktop/notepad/permission", json={"enabled": False})
        blocked = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert blocked.json()["launched"] is False
        assert launcher.generations == []
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        allowed = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert allowed.json()["launched"] is True
        assert len(launcher.generations) == 1


def test_denial_and_cancellation_do_not_launch(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        denied_id = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        denied = client.post("/api/desktop/notepad/confirm", json={"action_id": denied_id, "approved": False})
        assert denied.json()["launched"] is False
        assert denied.json()["detail"] == "The action was denied."
        retried = client.post("/api/desktop/notepad/confirm", json={"action_id": denied_id, "approved": True})
        assert retried.json()["launched"] is False
        waiting = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        stopped = client.post("/api/emergency-stop")
        assert stopped.json() == {"requested": True}
        cancelled = client.post("/api/desktop/notepad/confirm", json={"action_id": waiting, "approved": True})
        assert cancelled.json()["launched"] is False
        assert launcher.generations == []


def test_expired_approval_does_not_launch(tmp_path: Path):
    launcher = _Launcher()
    now = {"t": 0.0}
    halt, policy, opener = _opener(tmp_path, launcher, clock=lambda: now["t"], ttl_s=120)
    policy.enabled[Permission.APP_CONTROL] = True
    requested = opener.request()
    action_id = requested["pending"]["action_id"]
    now["t"] = 120.0
    expired = opener.confirm(action_id, True)
    assert expired["launched"] is False
    assert expired["detail"] == "That approval has expired."
    assert launcher.generations == []
    assert halt.approval_state(action_id) == "denied"


def test_forged_action_ids_are_rejected(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        real_id = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        forged = [
            "a" * 32,
            "g" * 32,
            r"C:\Windows\System32\cmd.exe",
            "notepad",
            real_id[:-1] + ("0" if real_id[-1] != "0" else "1"),
        ]
        for action_id in forged:
            response = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
            assert response.status_code in {200, 422}
            if response.status_code == 200:
                assert response.json()["launched"] is False
        assert launcher.generations == []
        approved = client.post("/api/desktop/notepad/confirm", json={"action_id": real_id, "approved": True})
        assert approved.json()["launched"] is True
        assert len(launcher.generations) == 1


def test_duplicate_approval_is_single_use_under_races(tmp_path: Path):
    launcher = _Launcher()
    halt, policy, opener = _opener(tmp_path, launcher)
    del halt
    policy.enabled[Permission.APP_CONTROL] = True
    action_id = opener.request()["pending"]["action_id"]
    barrier = threading.Barrier(2)
    results = []

    def confirm() -> None:
        barrier.wait()
        results.append(opener.confirm(action_id, True))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [item["launched"] for item in results].count(True) == 1
    assert len(launcher.generations) == 1
    assert opener.confirm(action_id, True)["launched"] is False
    assert len(launcher.generations) == 1


def test_emergency_stop_and_resume_do_not_replay(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        action_id = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        client.post("/api/emergency-stop")
        resumed = client.post("/api/resume")
        assert resumed.json() == {"resumed": True, "replayed": False}
        replayed = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert replayed.json()["launched"] is False
        assert launcher.generations == []
        fresh = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        opened = client.post("/api/desktop/notepad/confirm", json={"action_id": fresh, "approved": True})
        assert opened.json()["launched"] is True
        assert len(launcher.generations) == 1
        client.post("/api/emergency-stop")
        client.post("/api/resume")
        assert client.post("/api/desktop/notepad/confirm", json={"action_id": fresh, "approved": True}).json()["launched"] is False
        assert len(launcher.generations) == 1


def test_failure_does_not_launch_or_echo_the_exception(tmp_path: Path):
    def explode(_generation: int) -> bool:
        raise RuntimeError(r"C:\secret\notepad.exe")

    app = create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=explode,
    )
    with TestClient(app) as client:
        client.post("/api/desktop/notepad/permission", json={"enabled": True})
        action_id = client.post("/api/desktop/notepad", json={}).json()["pending"]["action_id"]
        failed = client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True})
        assert failed.json()["launched"] is False
        assert "secret" not in failed.text
        assert client.post("/api/desktop/notepad/confirm", json={"action_id": action_id, "approved": True}).json()["launched"] is False


def test_simulator_and_planner_cannot_trigger_notepad(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    app.state.policy.enabled[Permission.APP_CONTROL] = True
    with TestClient(app) as client:
        proposed = client.post(
            "/api/desktop/simulator/propose",
            json={
                "action_type": "open_application",
                "application": "Notepad",
                "description": "Notepad window.",
                "summary": "Open Notepad from the Start menu.",
                "risk": "Medium",
            },
        )
        action_id = proposed.json()["proposal"]["action_id"]
        decided = client.post("/api/desktop/simulator/decide", json={"action_id": action_id, "approved": True})
        assert decided.json()["real_desktop"] is False
        assert decided.json()["simulated"] is True
        planned = client.post("/api/desktop/plan", json={"task": "Plan how to open Notepad and write a note"})
        assert planned.status_code == 200
        agent = client.post("/api/agent/plan", json={"task": "open notepad and write a note"})
        assert agent.status_code == 200
        assert launcher.generations == []
        assert "notepad" not in app.state.skills.ids()
        assert "notepad" not in SUPPORTED_SKILLS
        assert "notepad" not in _FIELDS
        assert EXECUTION_ENABLED is False


def test_notepad_routes_keep_authentication(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    token = app.state.api_token
    with RAW_TEST_CLIENT(app, base_url="http://127.0.0.1:8765") as client:
        checks = [
            client.get("/api/desktop/notepad"),
            client.post("/api/desktop/notepad", json={"application": "cmd.exe", "path": r"C:\Windows\System32\cmd.exe"}),
            client.post(
                "/api/desktop/notepad/confirm",
                json={"action_id": "a" * 32, "approved": True, "path": r"C:\evil\app.exe"},
            ),
            client.post("/api/desktop/notepad/permission", json={"enabled": True, "application": "calc.exe"}),
        ]
        for response in checks:
            assert response.status_code == 401
            assert response.json()["detail"] == "Unauthorized."
            assert token not in response.text
    assert launcher.generations == []
    assert app.state.policy.enabled[Permission.APP_CONTROL] is False


def test_notepad_section_is_separate_from_the_simulator(tmp_path: Path):
    launcher = _Launcher()
    app = _app(tmp_path, launcher)
    with TestClient(app) as client:
        page = client.get("/").text
        simulator = page.split('id="desktop-simulator"', 1)[1].split("</section>", 1)[0]
        section = page.split('id="notepad-action"', 1)[1].split("</section>", 1)[0]
        assert "Real desktop action" not in simulator
        assert "Real desktop action" in section
        assert "Open Notepad" in section
        assert ">Approve<" in section
        assert ">Deny<" in section
        assert "Enable Open Notepad" in section
        assert "<input" not in section
        assert "speakText" not in section


def test_opener_stays_isolated_from_planner_and_simulator():
    source = Path("jarvis/desktop/notepad.py").read_text(encoding="utf-8")
    for banned in (
        "import subprocess",
        "from subprocess",
        "os.system",
        "shell=True",
        "ShellExecute",
        "startfile",
        "Popen",
        "powershell",
        "cmd.exe",
        "pyautogui",
        "SkillExecutor",
        "DesktopSimulator",
        "propose_desktop_plan",
    ):
        assert banned not in source
    for path in (
        "jarvis/desktop/simulator.py",
        "jarvis/desktop/plan.py",
        "jarvis/desktop/safety.py",
        "jarvis/skills/orchestrator.py",
        "jarvis/skills/planner.py",
    ):
        text = Path(path).read_text(encoding="utf-8")
        assert "NotepadOpener" not in text
        assert "launch_notepad" not in text
    wiring = Path("jarvis/main.py").read_text(encoding="utf-8")
    assert "NotepadOpener(halt, audit, policy" in wiring
    assert "NotepadOpener(skill_executor" not in wiring
    assert "DesktopSimulator(desktop_safety)" in wiring
