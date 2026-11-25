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
_REAL_IDENTITIES = windows_module.list_notepad_identities
_REAL_FOCUS = windows_module.focus_handle


class FakeWindows:
    def __init__(self) -> None:
        self.calls: list = []
        self.scene = {
            "handle": 1,
            "pid": 10,
            "image": "notepad.exe",
            "class_name": "Notepad",
            "title": "Untitled - Notepad",
            "protected": False,
            "elements": [{"name": "Quarterly revenue figures"}],
        }
        self.others: list[dict] = [
            {
                "handle": 9,
                "pid": 99,
                "image": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Secret bank password",
                "protected": False,
            },
            {
                "handle": 8,
                "pid": 12,
                "image": "notepad.exe",
                "class_name": "Notepad",
                "title": "Sign in - Notepad",
                "protected": False,
            },
        ]

    def observe(self) -> dict:
        self.calls.append("observe")
        rows = [json.loads(json.dumps(self.scene)), *json.loads(json.dumps(self.others))]
        return {"windows": rows}

    def focus(self, handle: int, allow) -> bool:
        if not allow():
            return False
        self.calls.append(("focus", handle))
        return True


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tests must not focus a real window")

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


def _app(tmp_path: Path, desktop: FakeWindows):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("window switching must not launch Notepad")),
        focus_port=desktop,
    )


def _enable(client: TestClient) -> None:
    response = client.post("/api/desktop/focus/permission", json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"permission": "window_switch", "enabled": True}


def _find(client: TestClient) -> dict:
    preview = client.post("/api/desktop/focus/preview", json={"action": "observe"}).json()
    assert preview["pending"]["summary"] == "List open Notepad windows."
    assert preview["performed"] is False
    approved = client.post(
        "/api/desktop/focus/confirm",
        json={"action_id": preview["pending"]["action_id"], "approved": True},
    ).json()
    assert approved["performed"] is True
    body = json.dumps(approved)
    assert "Secret bank password" not in body
    assert "Sign in - Notepad" not in body
    assert "Quarterly revenue figures" not in body
    assert "handle" not in body
    return approved


def test_permission_is_off_and_focus_waits_for_its_own_approval(tmp_path: Path):
    desktop = FakeWindows()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        state = client.get("/api/desktop/focus").json()
        assert state["permission_enabled"] is False
        assert state["performed"] is False
        refused = client.post("/api/desktop/focus/preview", json={"action": "focus_window"}).json()
        assert refused["text"] == "Window switching is not enabled."
        assert desktop.calls == []
        _enable(client)
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["automate"] is False
        assert permissions["app_control"] is False
        assert app.state.policy.enabled[Permission.AUTOMATE] is False
        assert app.state.policy.enabled[Permission.APP_CONTROL] is False
        found = _find(client)
        window = found["observation"]["windows"][0]
        assert window["title"] == "Untitled - Notepad"
        assert window["application"] == "Notepad"
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        assert preview["pending"]["summary"] == "Focus Untitled - Notepad."
        assert preview["performed"] is False
        assert ("focus", 1) not in desktop.calls
        focused = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert focused["text"] == "Notepad was focused."
        assert focused["performed"] is True
        assert ("focus", 1) in desktop.calls
        assert ("focus", 9) not in desktop.calls
        again = client.get("/api/desktop/focus").json()
        assert again["performed"] is False
        assert desktop.calls.count(("focus", 1)) == 1
    assert EXECUTION_ENABLED is False


def test_other_windows_and_an_empty_list_are_not_focused(tmp_path: Path):
    desktop = FakeWindows()
    desktop.scene["title"] = "*Notes one, More tab - Notepad"
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        found = _find(client)
        assert [row["title"] for row in found["observation"]["windows"]] == ["*Notes one, More tab - Notepad"]
        desktop.scene["image"] = "explorer.exe"
        desktop.others = []
        missing = _find_raw(client)
        assert missing["text"] == "No Notepad window was found."
        assert missing["observation"] is None
        assert missing["performed"] is False
        assert ("focus", 1) not in desktop.calls


def _find_raw(client: TestClient) -> dict:
    preview = client.post("/api/desktop/focus/preview", json={"action": "observe"}).json()
    return client.post(
        "/api/desktop/focus/confirm",
        json={"action_id": preview["pending"]["action_id"], "approved": True},
    ).json()


def test_denial_cancel_disable_stop_and_resume_do_not_focus(tmp_path: Path):
    desktop = FakeWindows()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _find(client)["observation"]["windows"][0]
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        denied = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["text"] == "The action was denied."
        assert ("focus", 1) not in desktop.calls
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        cancelled = client.post("/api/desktop/focus/cancel", json={}).json()
        assert cancelled["text"] == "The action was cancelled."
        assert client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        assert client.post("/api/desktop/focus/permission", json={"enabled": False}).status_code == 200
        blocked = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert blocked["text"] == "Window switching is not enabled."
        assert ("focus", 1) not in desktop.calls
        _enable(client)
        assert client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        stale = client.post("/api/desktop/focus/confirm", json={"action_id": action_id, "approved": True}).json()
        assert stale["performed"] is False
        assert ("focus", 1) not in desktop.calls
        assert client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()["text"] == "Find the Notepad windows before focusing."


def test_expired_forged_and_changed_targets_are_rejected(tmp_path: Path):
    desktop = FakeWindows()
    app = _app(tmp_path, desktop)
    clock = {"now": 0.0}
    app.state.window_switcher._clock = lambda: clock["now"]
    with TestClient(app) as client:
        _enable(client)
        assert client.post("/api/desktop/focus/preview", json={"action": "observe", "x": 1}).status_code == 422
        assert client.post("/api/desktop/focus/preview", json={"action": "type_text"}).json()["text"] == "That window action is not allowed."
        assert client.post("/api/desktop/focus/preview", json={"action": "open_application"}).json()["text"] == "That window action is not allowed."
        window = _find(client)["observation"]["windows"][0]
        forged = "ab" * 16
        assert client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": forged},
        ).json()["text"] == "That window is not available."
        assert client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": "0123456789abcdef0123456789abcdef", "approved": True},
        ).json()["text"] == "That action is not waiting for approval."
        assert client.post("/api/desktop/focus/confirm", json={"action_id": "short", "approved": True}).status_code == 422
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        clock["now"] = 120.0
        expired = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert expired["text"] == "That approval has expired."
        assert ("focus", 1) not in desktop.calls
        clock["now"] = 200.0
        window = _find(client)["observation"]["windows"][0]
        originals = {
            "handle": 1,
            "pid": 10,
            "title": "Untitled - Notepad",
            "image": "notepad.exe",
            "class_name": "Notepad",
        }
        for field, value in (
            ("handle", 50),
            ("pid", 77),
            ("title", "Other - Notepad"),
            ("image", "cmd.exe"),
            ("class_name", "Chrome_WidgetWin_1"),
        ):
            preview = client.post(
                "/api/desktop/focus/preview",
                json={"action": "focus_window", "window_id": window["window_id"]},
            ).json()
            desktop.scene[field] = value
            changed = client.post(
                "/api/desktop/focus/confirm",
                json={"action_id": preview["pending"]["action_id"], "approved": True},
            ).json()
            assert changed["text"] == "The window changed before it could be focused."
            assert ("focus", 1) not in desktop.calls
            desktop.scene[field] = originals[field]
        duplicate_preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        done = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": duplicate_preview["pending"]["action_id"], "approved": True},
        ).json()
        assert done["performed"] is True
        second = client.post(
            "/api/desktop/focus/confirm",
            json={"action_id": duplicate_preview["pending"]["action_id"], "approved": True},
        ).json()
        assert second["performed"] is False
        assert desktop.calls.count(("focus", 1)) == 1
        assert "Untitled - Notepad" not in (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")


def test_concurrent_focus_approvals_run_at_most_once(tmp_path: Path):
    desktop = FakeWindows()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _find(client)["observation"]["windows"][0]
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def confirm() -> None:
            barrier.wait(timeout=2)
            results.append(app.state.window_switcher.confirm(action_id, True))

        threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert desktop.calls.count(("focus", 1)) == 1
        assert sum(item["performed"] is True for item in results) == 1
        assert app.state.window_switcher.confirm(action_id, True)["performed"] is False


def test_stop_during_focus_is_not_replayed_on_resume(tmp_path: Path):
    desktop = FakeWindows()
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
        window = _find(client)["observation"]["windows"][0]
        preview = client.post(
            "/api/desktop/focus/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        result: dict = {}

        def run() -> None:
            result.update(app.state.window_switcher.confirm(action_id, True))

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(timeout=2)
        app.state.halt.emergency_stop()
        release.set()
        worker.join(timeout=2)
        app.state.window_switcher.observe_stop()
        assert ("focus", 1) not in desktop.calls
        assert result["performed"] is False
        app.state.halt.resume()
        assert app.state.window_switcher.confirm(action_id, True)["performed"] is False
        assert ("focus", 1) not in desktop.calls


class _FakeWin32:
    """Stand-in for user32 and kernel32. It never focuses a real window."""

    def __init__(self, specs: dict[int, dict], paths: dict[int, str]) -> None:
        self.specs = specs
        self.paths = paths
        self.calls: list = []

    def EnumWindows(self, callback, _lparam) -> bool:
        for hwnd, spec in self.specs.items():
            if spec.get("top") and callback(hwnd, 0) is False:
                break
        return True

    def EnumChildWindows(self, parent, callback, _lparam) -> bool:
        for hwnd in self.specs.get(int(parent), {}).get("children", []):
            if callback(hwnd, 0) is False:
                break
        return True

    def GetClassNameW(self, hwnd, buffer, size) -> int:
        name = self.specs.get(int(hwnd), {}).get("class", "")[: max(size - 1, 0)]
        buffer.value = name
        return len(name)

    def GetWindowTextW(self, hwnd, buffer, size) -> int:
        name = self.specs.get(int(hwnd), {}).get("text", "")[: max(size - 1, 0)]
        buffer.value = name
        return len(name)

    def GetWindowThreadProcessId(self, hwnd, pid_out) -> int:
        pid_out._obj.value = self.specs.get(int(hwnd), {}).get("pid", 0)
        return 1

    def GetWindowLongW(self, hwnd, _index) -> int:
        return int(self.specs.get(int(hwnd), {}).get("style", 0))

    def GetAncestor(self, hwnd, _flags) -> int:
        return int(self.specs.get(int(hwnd), {}).get("root", hwnd))

    def OpenProcess(self, _access, _inherit, pid) -> int:
        return int(pid) if int(pid) in self.paths else 0

    def QueryFullProcessImageNameW(self, process, _flags, buffer, size) -> bool:
        path = self.paths.get(int(process), "")
        if not path:
            return False
        buffer.value = path
        size._obj.value = len(path)
        return True

    def CloseHandle(self, _handle) -> bool:
        return True

    def SetForegroundWindow(self, hwnd) -> bool:
        self.calls.append(("foreground", int(hwnd)))
        return True

    def SendMessageW(self, hwnd, message, wparam, _lparam) -> int:
        self.calls.append(("message", int(hwnd), int(message), int(wparam)))
        return 1


def test_mocked_win32_identities_exclude_other_windows_and_child_text(monkeypatch):
    specs = {
        1: {
            "top": True,
            "class": "Notepad",
            "pid": 10,
            "text": "*Notes one, More tab - Notepad",
            "children": [3],
            "root": 1,
        },
        3: {"class": "RichEditD2DPT", "pid": 10, "root": 1, "text": "Quarterly revenue figures"},
        8: {"top": True, "class": "Notepad", "pid": 12, "text": "Sign in - Notepad", "root": 8, "children": []},
        9: {"top": True, "class": "Chrome_WidgetWin_1", "pid": 99, "text": "Secret bank password", "root": 9, "children": []},
    }
    paths = {
        10: r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad\Notepad\Notepad.exe",
        12: r"C:\Windows\System32\notepad.exe",
        99: r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    }
    api = _FakeWin32(specs, paths)
    monkeypatch.setattr(windows_module, "_libraries", lambda: (api, api))
    monkeypatch.setattr(windows_module, "list_notepad_windows", _REAL_LIST)
    identities = _REAL_IDENTITIES()
    body = json.dumps(identities)
    assert [row["title"] for row in identities["windows"]] == ["*Notes one, More tab - Notepad"]
    assert "elements" not in identities["windows"][0]
    for hidden in ("Secret bank password", "Sign in - Notepad", "Quarterly revenue figures"):
        assert hidden not in body
    assert _REAL_FOCUS(1, lambda: False) is False
    assert api.calls == []
    assert _REAL_FOCUS(9, lambda: True) is False
    assert ("foreground", 9) not in api.calls
    assert _REAL_FOCUS(1, lambda: True) is True
    assert api.calls == [("foreground", 1)]


def test_identities_omit_child_text_and_the_feature_stays_isolated(tmp_path: Path, monkeypatch):
    def listed() -> dict:
        return {
            "windows": [
                {
                    "handle": 1,
                    "pid": 10,
                    "image": "notepad.exe",
                    "class_name": "Notepad",
                    "title": "Notes - Notepad",
                    "protected": False,
                    "elements": [{"name": "Quarterly revenue figures"}],
                }
            ]
        }

    monkeypatch.setattr(windows_module, "list_notepad_windows", listed)
    identities = _REAL_IDENTITIES()
    assert "elements" not in identities["windows"][0]
    assert "Quarterly revenue figures" not in json.dumps(identities)
    desktop = FakeWindows()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        page = client.get("/").text
        section = page.split('id="window-switch"', 1)[1].split("</section>", 1)[0]
        control = page.split('id="desktop-control"', 1)[1].split("</section>", 1)[0]
        assert "Preview focus" in section
        assert "Window switching is off." in section
        assert "does not type, click, or open programs" in section
        assert "Execute" not in section
        assert "id=\"window-switch\"" not in control
        assert "speakText" not in section
        goal = client.post("/api/agent/goal", json={"task": "List the workspace."})
        assert goal.status_code == 200
        assert desktop.calls == []
    assert _REAL_FOCUS(1, lambda: False) is False
    source = Path("jarvis/desktop/switch.py").read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "SendInput" not in source
    assert "SetCursorPos" not in source
    assert "mouse_event" not in source
    assert "type_into_handle" not in source
    assert "click_handle" not in source
    assert "jarvis.desktop.control" not in source
    assert "jarvis.desktop.simulator" not in source
    assert "jarvis.desktop.notepad" not in source
    assert "jarvis.agent" not in source
