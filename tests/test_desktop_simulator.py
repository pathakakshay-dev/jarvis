import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.desktop.safety import EXECUTION_ENABLED, DesktopSafety
from jarvis.desktop.simulator import DesktopSimulator
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission


def _pair(tmp_path: Path, *, clock=None):
    halt = HaltController()
    audit = AuditLog(tmp_path / "audit.jsonl")
    safety = DesktopSafety(halt, audit, clock=clock)
    return halt, safety, DesktopSimulator(safety)


def _open(**overrides) -> dict:
    payload = {
        "action_type": "open_application",
        "application": "Notepad",
        "description": "Notepad window.",
        "summary": "Open Notepad from the Start menu.",
        "risk": "Medium",
    }
    payload.update(overrides)
    return payload


def _focus(**overrides) -> dict:
    payload = {
        "action_type": "focus_application",
        "application": "Notepad",
        "description": "Notepad window.",
        "summary": "Focus the Notepad window.",
        "risk": "Low",
    }
    payload.update(overrides)
    return payload


def _close(**overrides) -> dict:
    payload = {
        "action_type": "close_application",
        "application": "Notepad",
        "description": "Notepad window.",
        "summary": "Close the Notepad window.",
        "risk": "High",
    }
    payload.update(overrides)
    return payload


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


def test_supported_actions_change_only_the_mock_desktop(tmp_path: Path):
    _halt, safety, simulator = _pair(tmp_path)
    opened = simulator.propose(_open())
    assert opened["ok"] is True
    assert opened["mode"] == "mock"
    assert opened["real_desktop"] is False
    assert opened["execution_permitted"] is False
    assert opened["proposal"]["risk"] == "Medium"
    assert "Simulated results do not represent real Windows actions." in opened["proposal"]["preview"]
    open_id = opened["proposal"]["action_id"]
    applied = simulator.decide(open_id, True)
    assert applied["simulated"] is True
    assert applied["open_applications"] == ["Notepad"]
    assert applied["focused"] == ""
    focused = simulator.decide(simulator.propose(_focus())["proposal"]["action_id"], True)
    assert focused["simulated"] is True
    assert focused["focused"] == "Notepad"
    closed = simulator.propose(_close())
    assert closed["proposal"]["risk"] == "High"
    finished = simulator.decide(closed["proposal"]["action_id"], True)
    assert finished["simulated"] is True
    assert finished["open_applications"] == []
    assert finished["focused"] == ""
    assert safety.get(closed["proposal"]["action_id"])["review_required"] is True
    assert safety.reject_execution()["execution_permitted"] is False
    assert EXECUTION_ENABLED is False


def test_invalid_action_and_target_do_not_change_the_mock(tmp_path: Path):
    _halt, _safety, simulator = _pair(tmp_path)
    unknown = simulator.propose(_open(action_type="click_mouse", risk="High"))
    target = simulator.propose(_open(application="C:\\Windows\\notepad.exe"))
    missing = dict(_open())
    missing.pop("summary")
    malformed = simulator.propose(missing)
    assert unknown["ok"] is False and unknown["real_desktop"] is False
    assert target["ok"] is False
    assert malformed["ok"] is False
    assert simulator.snapshot()["open_applications"] == []
    assert simulator.snapshot()["history"] == []


def test_denial_duplicate_expired_and_forged_ids_do_not_simulate(tmp_path: Path):
    box = {"now": 50.0}
    halt, safety, simulator = _pair(tmp_path, clock=lambda: box["now"])
    first = simulator.propose(_open())["proposal"]["action_id"]
    denied = simulator.decide(first, False)
    assert denied["ok"] is False
    assert denied["simulated"] is False
    assert simulator.snapshot()["open_applications"] == []
    second = simulator.propose(_open())["proposal"]["action_id"]
    assert simulator.decide(second, True)["simulated"] is True
    duplicate = simulator.decide(second, True)
    assert duplicate["ok"] is False
    assert duplicate["open_applications"] == ["Notepad"]
    assert len(simulator.snapshot()["history"]) == 1
    third = simulator.propose(_focus())["proposal"]["action_id"]
    box["now"] = 170.0
    expired = simulator.decide(third, True)
    assert expired["ok"] is False
    assert expired["focused"] == ""
    forged = simulator.decide("not-a-real-action", True)
    assert forged["ok"] is False
    assert forged["simulated"] is False
    assert "not-a-real-action" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert safety.get(second)["execution_permitted"] is False
    assert halt.approval_state(second) == "approved"


def test_high_risk_requires_an_explicit_approval(tmp_path: Path):
    _halt, safety, simulator = _pair(tmp_path)
    open_id = simulator.propose(_open())["proposal"]["action_id"]
    assert simulator.decide(open_id, True)["simulated"] is True
    close_id = simulator.propose(_close())["proposal"]["action_id"]
    automatic = simulator.decide(close_id, True, automatic=True)
    assert automatic["ok"] is False
    assert "High-risk" in automatic["detail"]
    assert simulator.snapshot()["open_applications"] == ["Notepad"]
    explicit = simulator.decide(close_id, True)
    assert explicit["simulated"] is True
    assert explicit["real_desktop"] is False
    assert explicit["open_applications"] == []
    assert safety.get(close_id)["review_required"] is True
    assert safety.get(close_id)["state"] == "approved"


def test_stop_and_resume_do_not_replay(tmp_path: Path):
    halt, _safety, simulator = _pair(tmp_path)
    open_id = simulator.propose(_open())["proposal"]["action_id"]
    assert simulator.decide(open_id, True)["open_applications"] == ["Notepad"]
    pending = simulator.propose(_focus())["proposal"]["action_id"]
    halt.emergency_stop()
    simulator.observe_stop()
    assert simulator.decide(pending, True)["simulated"] is False
    assert simulator.snapshot()["focused"] == ""
    assert simulator.snapshot()["open_applications"] == ["Notepad"]
    halt.resume()
    simulator.observe_stop()
    replay = simulator.decide(open_id, True)
    assert replay["ok"] is False
    assert replay["replayed"] is False
    assert len(simulator.snapshot()["history"]) == 1
    fresh = simulator.propose(_focus())
    assert fresh["ok"] is True
    assert fresh["proposal"]["action_id"] != pending
    assert simulator.decide(fresh["proposal"]["action_id"], True)["focused"] == "Notepad"


def test_mock_state_is_isolated_and_reset_does_not_touch_other_data(tmp_path: Path):
    halt, safety, simulator = _pair(tmp_path)
    other = DesktopSimulator(DesktopSafety(HaltController(), AuditLog(tmp_path / "other.jsonl")))
    open_id = simulator.propose(_open())["proposal"]["action_id"]
    assert simulator.decide(open_id, True)["simulated"] is True
    assert other.snapshot()["open_applications"] == []
    separate = DesktopSimulator(safety)
    assert separate.snapshot()["open_applications"] == []
    assert separate.decide(open_id, True)["simulated"] is False
    note = tmp_path / "workspace" / "note.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text("keep", encoding="utf-8")
    halt.generation = halt.generation
    reset = simulator.reset()
    assert reset["open_applications"] == []
    assert reset["history"] == []
    assert reset["real_desktop"] is False
    assert note.read_text(encoding="utf-8") == "keep"
    assert halt.halted is False
    again = simulator.decide(open_id, True)
    assert again["simulated"] is False
    assert simulator.snapshot()["open_applications"] == []


def test_simulator_failures_are_closed(tmp_path: Path):
    _halt, safety, simulator = _pair(tmp_path)

    def explode(_payload):
        raise RuntimeError("secret traceback")

    safety.propose = explode
    failed = simulator.propose(_open())
    assert failed["ok"] is False
    assert failed["simulated"] is False
    assert "secret traceback" not in json.dumps(failed)
    assert simulator.snapshot()["open_applications"] == []
    closed = simulator.propose(_focus())
    assert closed["focused"] == ""


def test_concurrent_simulation_applies_once(tmp_path: Path):
    _halt, _safety, simulator = _pair(tmp_path)
    action_id = simulator.propose(_open())["proposal"]["action_id"]
    barrier = threading.Barrier(2)
    results = []

    def decide() -> None:
        barrier.wait()
        results.append(simulator.decide(action_id, True))

    threads = [threading.Thread(target=decide) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for item in results if item["simulated"]) == 1
    assert simulator.snapshot()["open_applications"] == ["Notepad"]
    assert len(simulator.snapshot()["history"]) == 1


def test_simulator_api_is_mock_only(tmp_path: Path):
    fake = FakeProvider(reply="local reply")
    app = create_app(settings=_settings(tmp_path), fake=fake, start_hotkeys_flag=False, transcriber=object(), speaker=object())
    note = tmp_path / "workspace" / "note.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text("keep", encoding="utf-8")
    app.state.policy.enabled[Permission.READ] = True
    with TestClient(app) as client:
        state = client.get("/api/desktop/simulator")
        assert state.status_code == 200
        assert state.json()["mode"] == "mock"
        assert state.json()["execution"] == "disabled"
        assert state.json()["real_desktop"] is False
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert "/api/desktop/simulator/execute" not in paths
        assert client.post("/api/desktop/simulator/execute").status_code == 404
        proposed = client.post("/api/desktop/simulator/propose", json=_open(description="private simulator note."))
        assert proposed.status_code == 200
        body = proposed.json()
        assert body["ok"] is True
        assert body["proposal"]["application"] == "Notepad"
        action_id = body["proposal"]["action_id"]
        extra = client.post("/api/desktop/simulator/propose", json={**_open(), "skill": "write_file"})
        assert extra.status_code == 422
        approved = client.post("/api/desktop/simulator/decide", json={"action_id": action_id, "approved": True})
        assert approved.json()["simulated"] is True
        assert approved.json()["real_desktop"] is False
        assert app.state.skill_executor.pending() == []
        reset = client.post("/api/desktop/simulator/reset", json={})
        assert reset.json()["open_applications"] == []
        assert app.state.policy.enabled[Permission.READ] is True
        assert note.read_text(encoding="utf-8") == "keep"
        assert app.state.halt.halted is False
        stopped = client.post("/api/emergency-stop")
        assert stopped.json() == {"requested": True}
        resumed = client.post("/api/resume")
        assert resumed.json()["replayed"] is False
        assert client.post("/api/desktop/simulator/decide", json={"action_id": action_id, "approved": True}).json()["simulated"] is False
        page = client.get("/").text
        section = page.split('id="desktop-simulator"', 1)[1].split("</section>", 1)[0]
        assert "Simulator mode: MOCK ONLY" in section
        assert "do not represent real Windows actions" in section
        assert ">Approve<" in section
        assert ">Deny<" in section
        assert "Reset simulation" in section
        audit = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
        assert "private simulator note." not in audit
        assert "Desktop simulation reset" in audit
        assert fake.calls == 0


def test_default_focus_notepad_proposal_is_mock_only(tmp_path: Path):
    launches = []

    def launcher(generation: int) -> bool:
        launches.append(generation)
        return True

    app = create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=launcher,
    )
    with TestClient(app) as client:
        section = client.get("/").text.split('id="desktop-simulator"', 1)[1].split("</section>", 1)[0]
        assert 'value="focus_application"' in section
        assert "<option>Notepad</option>" in section
        assert 'value="Notepad window."' in section
        assert 'value="Focus the Notepad window."' in section
        proposed = client.post(
            "/api/desktop/simulator/propose",
            json={
                "action_type": "focus_application",
                "application": "Notepad",
                "description": "Notepad window.",
                "summary": "Focus the Notepad window.",
                "risk": "Low",
            },
        )
        body = proposed.json()
        assert proposed.status_code == 200
        assert body["ok"] is True
        assert body["mode"] == "mock"
        assert body["real_desktop"] is False
        assert body["simulated"] is False
        assert body["execution_permitted"] is False
        assert body["proposal"]["action_type"] == "focus_application"
        assert body["proposal"]["application"] == "Notepad"
        assert body["proposal"]["risk"] == "Low"
        blank = client.post(
            "/api/desktop/simulator/propose",
            json={
                "action_type": "focus_application",
                "application": "Notepad",
                "description": "",
                "summary": "",
                "risk": "Low",
            },
        )
        assert blank.json()["ok"] is False
        assert blank.json()["real_desktop"] is False
        assert blank.json()["detail"] == "That desktop action is not valid."
        foreign = client.post("/api/desktop/simulator/propose", json=_focus(application="cmd.exe"))
        assert foreign.json()["ok"] is False
        assert foreign.json()["detail"] == "That desktop target is not allowed."
        assert launches == []
        assert EXECUTION_ENABLED is False


def test_simulator_source_has_no_desktop_control_path():
    source = Path("jarvis/desktop/simulator.py").read_text(encoding="utf-8")
    for banned in (
        "import subprocess",
        "from subprocess",
        "import ctypes",
        "import os",
        "pyautogui",
        "SkillExecutor",
        "skill_executor",
        "ShellExecute",
        "os.system",
        "psutil",
        "win32",
        "SetCursorPos",
        "keybd_event",
        "ImageGrab",
        "mouse_event",
    ):
        assert banned not in source
    wiring = Path("jarvis/main.py").read_text(encoding="utf-8")
    assert "DesktopSimulator(desktop_safety)" in wiring
    assert "DesktopSimulator(skill_executor)" not in wiring
