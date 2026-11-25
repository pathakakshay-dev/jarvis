"""Desktop Action Engine tests. These never touch the real desktop."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.desktop import windows as windows_module
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.policy import Permission

_REAL_LIST = windows_module.list_notepad_windows
_REAL_FOCUS = windows_module.focus_handle
_REAL_TYPE = windows_module.type_into_handle
_REAL_CLICK = windows_module.click_handle
_REAL_SELECT = windows_module.select_all_handle


class FakeDesktop:
    def __init__(self) -> None:
        self.calls: list = []
        self.scene = {
            "handle": 1,
            "pid": 10,
            "image": "notepad.exe",
            "class_name": "Notepad",
            "title": "Untitled - Notepad",
            "protected": False,
            "elements": [
                {"handle": 2, "role": "edit", "name": "Text editor", "password": False, "class_name": "Edit"},
                {"handle": 3, "role": "button", "name": "Save", "password": False, "class_name": "Button"},
            ],
        }
        self.others: list[dict] = [
            {
                "handle": 9,
                "pid": 99,
                "image": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Secret bank password",
                "protected": False,
                "elements": [],
            }
        ]

    def observe(self) -> dict:
        self.calls.append("observe")
        return {"windows": [json.loads(json.dumps(self.scene)), *json.loads(json.dumps(self.others))]}

    def focus(self, handle: int, allow) -> bool:
        if not allow():
            return False
        self.calls.append(("focus", handle))
        return True

    def type_text(self, handle: int, text: str, allow) -> bool:
        if not allow():
            return False
        self.calls.append(("type", handle, text))
        return True

    def click(self, handle: int, allow) -> bool:
        if not allow():
            return False
        self.calls.append(("click", handle))
        return True

    def shortcut(self, handle: int, allow) -> bool:
        if not allow():
            return False
        self.calls.append(("shortcut", handle))
        return True


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tests must not touch the desktop")

    monkeypatch.setattr(windows_module, "list_notepad_windows", boom)
    monkeypatch.setattr(windows_module, "list_notepad_identities", boom)
    monkeypatch.setattr(windows_module, "focus_handle", boom)
    monkeypatch.setattr(windows_module, "type_into_handle", boom)
    monkeypatch.setattr(windows_module, "click_handle", boom)
    monkeypatch.setattr(windows_module, "select_all_handle", boom)


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


def _app(tmp_path: Path, desktop: FakeDesktop, launches: list | None = None):
    calls = launches if launches is not None else []

    def launcher(*_args, **_kwargs):
        calls.append("launch")
        raise AssertionError("desktop engine must not launch applications")

    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=launcher,
        calculator_launcher=launcher,
        engine_port=desktop,
    )


def _enable(client: TestClient) -> None:
    response = client.post("/api/desktop/engine/permission", json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"permission": "desktop_engine", "enabled": True}


def _inspect(client: TestClient) -> dict:
    proposed = client.post("/api/desktop/engine/propose", json={"steps": [{"action": "observe"}]}).json()
    assert proposed["can_start"] is True
    started = client.post(
        "/api/desktop/engine/start",
        json={"plan_id": proposed["task"]["plan_id"], "approved": True},
    ).json()
    assert started["pending"]["action"] == "observe"
    done = client.post(
        "/api/desktop/engine/confirm",
        json={"action_id": started["pending"]["action_id"], "approved": True},
    ).json()
    assert done["task"]["status"] == "completed"
    assert "Secret bank password" not in json.dumps(done)
    return done


def test_permission_off_blocks_and_sequence_needs_approval(tmp_path: Path):
    desktop = FakeDesktop()
    launches: list = []
    app = _app(tmp_path, desktop, launches)
    with TestClient(app) as client:
        state = client.get("/api/desktop/engine").json()
        assert state["permission_enabled"] is False
        refused = client.post("/api/desktop/engine/propose", json={"steps": [{"action": "observe"}]}).json()
        assert refused["text"] == "Desktop action engine is not enabled."
        assert desktop.calls == []
        _enable(client)
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["automate"] is False
        assert permissions["app_control"] is False
        assert app.state.policy.enabled[Permission.AUTOMATE] is False
        inspected = _inspect(client)
        window = inspected["observation"]["windows"][0]
        editor = window["elements"][0]
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={
                "steps": [
                    {"action": "focus_window", "window_id": window["window_id"]},
                    {"action": "type_text", "window_id": window["window_id"], "element_id": editor["element_id"], "text": "Hello"},
                ]
            },
        ).json()
        assert proposed["can_start"] is True
        assert proposed["pending"] is None
        assert ("focus", 1) not in desktop.calls
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        assert started["pending"]["summary"] == "Focus Untitled - Notepad."
        assert ("focus", 1) not in desktop.calls
        focused = client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": started["pending"]["action_id"], "approved": True},
        ).json()
        assert ("focus", 1) in desktop.calls
        assert focused["pending"]["action"] == "type_text"
        typed = client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": focused["pending"]["action_id"], "approved": True},
        ).json()
        assert typed["task"]["status"] == "completed"
        assert ("type", 2, "Hello") in desktop.calls
        assert client.get("/api/desktop/engine").json()["performed"] is False
    assert launches == []
    assert EXECUTION_ENABLED is False


def test_denial_cancel_disable_stop_and_resume_stop_the_task(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        denied = client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": started["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["task"]["status"] == "denied"
        assert ("focus", 1) not in desktop.calls
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        cancelled = client.post("/api/desktop/engine/cancel", json={}).json()
        assert cancelled["task"]["status"] == "cancelled"
        assert client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": started["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        assert client.post("/api/desktop/engine/permission", json={"enabled": False}).status_code == 200
        blocked = client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": started["pending"]["action_id"], "approved": True},
        ).json()
        assert blocked["task"]["status"] == "failed"
        assert ("focus", 1) not in desktop.calls
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        action_id = started["pending"]["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        stale = client.post("/api/desktop/engine/confirm", json={"action_id": action_id, "approved": True}).json()
        assert stale["performed"] is False
        assert ("focus", 1) not in desktop.calls


def test_expiry_forged_ids_and_target_changes_fail_closed(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    clock = {"now": 0.0}
    app.state.desktop_engine._clock = lambda: clock["now"]
    with TestClient(app) as client:
        _enable(client)
        assert client.post("/api/desktop/engine/propose", json={"steps": [{"action": "observe"}], "x": 1}).status_code == 422
        assert client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "open_application"}]},
        ).json()["text"] == "Opening an application uses its own approved action."
        assert client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "shell"}]},
        ).json()["text"] == "That desktop action is not allowed."
        window = _inspect(client)["observation"]["windows"][0]
        forged = "ab" * 16
        assert client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": forged}]},
        ).json()["text"] == "That desktop target is not allowed."
        assert client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": "0123456789abcdef0123456789abcdef", "approved": True},
        ).json()["performed"] is False
        assert client.post("/api/desktop/engine/confirm", json={"action_id": "short", "approved": True}).status_code == 422
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        clock["now"] = 120.0
        expired = client.post(
            "/api/desktop/engine/confirm",
            json={"action_id": started["pending"]["action_id"], "approved": True},
        ).json()
        assert expired["text"] == "That approval has expired."
        assert ("focus", 1) not in desktop.calls
        clock["now"] = 200.0
        window = _inspect(client)["observation"]["windows"][0]
        for field, value in (("handle", 50), ("pid", 77), ("title", "Other - Notepad"), ("image", "cmd.exe")):
            proposed = client.post(
                "/api/desktop/engine/propose",
                json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
            ).json()
            started = client.post(
                "/api/desktop/engine/start",
                json={"plan_id": proposed["task"]["plan_id"], "approved": True},
            ).json()
            desktop.scene[field] = value
            changed = client.post(
                "/api/desktop/engine/confirm",
                json={"action_id": started["pending"]["action_id"], "approved": True},
            ).json()
            assert changed["task"]["status"] == "failed"
            assert ("focus", 1) not in desktop.calls
            desktop.scene[field] = {"handle": 1, "pid": 10, "title": "Untitled - Notepad", "image": "notepad.exe"}[field]


def test_concurrent_approvals_run_at_most_once(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        action_id = started["pending"]["action_id"]
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def confirm() -> None:
            barrier.wait(timeout=2)
            results.append(app.state.desktop_engine.confirm(action_id, True))

        threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert desktop.calls.count(("focus", 1)) == 1
        assert sum(item.get("performed") is True for item in results) == 1
        assert app.state.desktop_engine.confirm(action_id, True)["performed"] is False
        assert app.state.desktop_engine.status()["task"]["status"] == "completed"


def test_stop_during_action_is_not_replayed(tmp_path: Path):
    desktop = FakeDesktop()
    entered = threading.Event()
    release = threading.Event()

    def focus(handle: int, allow) -> bool:
        if not allow():
            return False
        entered.set()
        assert release.wait(timeout=2)
        if not allow():
            return False
        desktop.calls.append(("focus", handle))
        return True

    desktop.focus = focus
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        proposed = client.post(
            "/api/desktop/engine/propose",
            json={"steps": [{"action": "focus_window", "window_id": window["window_id"]}]},
        ).json()
        started = client.post(
            "/api/desktop/engine/start",
            json={"plan_id": proposed["task"]["plan_id"], "approved": True},
        ).json()
        action_id = started["pending"]["action_id"]
        result: dict = {}

        def run() -> None:
            result.update(app.state.desktop_engine.confirm(action_id, True))

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(timeout=2)
        app.state.halt.emergency_stop()
        release.set()
        worker.join(timeout=2)
        app.state.desktop_engine.observe_stop()
        assert ("focus", 1) not in desktop.calls
        assert result.get("performed") is False
        app.state.halt.resume()
        assert app.state.desktop_engine.confirm(action_id, True)["performed"] is False
        assert ("focus", 1) not in desktop.calls


def test_protected_windows_and_password_fields_are_excluded(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        desktop.scene["title"] = "Sign in - Notepad"
        missing = _run_observe(client)
        assert missing["observation"] is None or missing["observation"]["windows"] == []
        assert "Sign in - Notepad" not in json.dumps(missing)
        desktop.scene["title"] = "Untitled - Notepad"
        desktop.scene["elements"][0]["password"] = True
        inspected = _inspect(client)
        roles = [element["role"] for element in inspected["observation"]["windows"][0]["elements"]]
        assert "edit" not in roles


def _run_observe(client: TestClient) -> dict:
    proposed = client.post("/api/desktop/engine/propose", json={"steps": [{"action": "observe"}]}).json()
    started = client.post(
        "/api/desktop/engine/start",
        json={"plan_id": proposed["task"]["plan_id"], "approved": True},
    ).json()
    return client.post(
        "/api/desktop/engine/confirm",
        json={"action_id": started["pending"]["action_id"], "approved": True},
    ).json()


def test_goal_planner_simulator_and_launchers_stay_disconnected(tmp_path: Path):
    desktop = FakeDesktop()
    launches: list = []
    app = _app(tmp_path, desktop, launches)
    with TestClient(app) as client:
        page = client.get("/").text
        section = page.split('id="desktop-engine"', 1)[1].split("</section>", 1)[0]
        control = page.split('id="desktop-control"', 1)[1].split("</section>", 1)[0]
        assert "Desktop Action Engine" in section
        assert "Propose task" in section
        assert "does not launch programs" in section
        assert "Execute" not in section
        assert "id=\"desktop-engine\"" not in control
        _enable(client)
        goal = client.post("/api/agent/goal", json={"task": "List the workspace."})
        assert goal.status_code == 200
        planned = client.post("/api/desktop/plan", json={"task": "Focus Notepad"})
        assert planned.status_code == 200
        assert desktop.calls == []
    assert launches == []
    assert _REAL_FOCUS(1, lambda: False) is False
    assert _REAL_TYPE(1, "Hello", lambda: False) is False
    assert _REAL_CLICK(1, lambda: False) is False
    assert _REAL_SELECT(1, lambda: False) is False
    source = Path("jarvis/desktop/engine.py").read_text(encoding="utf-8")
    for banned in (
        "import subprocess",
        "SendInput",
        "SetCursorPos",
        "mouse_event",
        "jarvis.desktop.notepad",
        "jarvis.desktop.calculator",
        "jarvis.desktop.control",
        "jarvis.desktop.switch",
        "jarvis.desktop.simulator",
        "jarvis.agent",
        "Permission",
    ):
        assert banned not in source
