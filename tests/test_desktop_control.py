import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.desktop import windows as windows_module
from jarvis.desktop.control import _accept_window
from jarvis.desktop.windows import _targets_from_children, visible_notepad_windows
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider

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
        calls.append("notepad")
        raise AssertionError("desktop control must not launch Notepad")

    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=launcher,
        desktop_port=desktop,
    )


def _enable(client: TestClient) -> None:
    response = client.post("/api/desktop/control/permission", json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"permission": "automate", "enabled": True}


def _approve_observe(client: TestClient) -> dict:
    preview = client.post("/api/desktop/control/preview", json={"action": "observe"}).json()
    assert preview["pending"]["action"] == "observe"
    assert preview["performed"] is False
    return client.post(
        "/api/desktop/control/confirm",
        json={"action_id": preview["pending"]["action_id"], "approved": True},
    ).json()


def _inspect(client: TestClient) -> dict:
    approved = _approve_observe(client)
    assert approved["performed"] is True
    assert "Secret bank password" not in json.dumps(approved)
    return approved


def test_permission_is_off_and_inspection_waits_for_approval(tmp_path: Path):
    desktop = FakeDesktop()
    launches: list = []
    app = _app(tmp_path, desktop, launches)
    with TestClient(app) as client:
        state = client.get("/api/desktop/control").json()
        assert state["permission_enabled"] is False
        assert state["performed"] is False
        refused = client.post("/api/desktop/control/preview", json={"action": "observe"}).json()
        assert refused["text"] == "Desktop control is not enabled."
        assert desktop.calls == []
        _enable(client)
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["automate"] is True
        assert permissions["app_control"] is False
        preview = client.post("/api/desktop/control/preview", json={"action": "observe"}).json()
        assert preview["pending"]["state"] == "waiting for approval"
        assert desktop.calls == []
        denied = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["text"] == "The action was denied."
        assert denied["performed"] is False
        assert desktop.calls == []
    assert launches == []
    assert EXECUTION_ENABLED is False


def test_type_and_focus_run_only_after_their_own_approval(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        inspected = _inspect(client)
        window = inspected["observation"]["windows"][0]
        editor = window["elements"][0]
        assert editor["role"] == "edit"
        assert "handle" not in editor
        preview = client.post(
            "/api/desktop/control/preview",
            json={
                "action": "type_text",
                "window_id": window["window_id"],
                "element_id": editor["element_id"],
                "text": "Hello",
            },
        ).json()
        assert preview["pending"]["summary"] == "Type into the Notepad text editor."
        assert preview["pending"]["text"] == "Hello"
        assert desktop.calls == ["observe"]
        typed = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert typed["performed"] is True
        assert typed["text"] == "Text was typed."
        assert ("type", 2, "Hello") in desktop.calls
        again = client.get("/api/desktop/control").json()
        assert again["performed"] is False
        assert desktop.calls.count(("type", 2, "Hello")) == 1
        focus = client.post(
            "/api/desktop/control/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        focused = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": focus["pending"]["action_id"], "approved": True},
        ).json()
        assert focused["text"] == "Notepad was focused."
        assert ("focus", 1) in desktop.calls


def test_denial_cancel_stop_and_resume_do_not_act(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        editor = window["elements"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": editor["element_id"], "text": "Hello"},
        ).json()
        cancelled = client.post("/api/desktop/control/cancel", json={}).json()
        assert cancelled["text"] == "The action was cancelled."
        assert client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        assert ("focus", 1) not in desktop.calls
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        stale = client.post("/api/desktop/control/confirm", json={"action_id": action_id, "approved": True}).json()
        assert stale["performed"] is False
        assert ("focus", 1) not in desktop.calls
        assert client.post(
            "/api/desktop/control/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()["text"] == "Inspect the window before choosing an action."


def test_changed_window_timeout_and_disabled_permission_block_the_action(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    clock = {"now": 0.0}
    app.state.desktop_control._clock = lambda: clock["now"]
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        editor = window["elements"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": editor["element_id"], "text": "Hello"},
        ).json()
        desktop.scene["title"] = "Other - Notepad"
        changed = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert changed["text"] == "The desktop changed before the action."
        assert ("type", 2, "Hello") not in desktop.calls
        desktop.scene["title"] = "Untitled - Notepad"
        window = _inspect(client)["observation"]["windows"][0]
        editor = window["elements"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "click_element", "window_id": window["window_id"], "element_id": editor["element_id"]},
        ).json()
        assert preview["text"] == "That desktop action is not valid."
        button = window["elements"][1]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "click_element", "window_id": window["window_id"], "element_id": button["element_id"]},
        ).json()
        clock["now"] = 120.0
        expired = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert expired["performed"] is False
        assert ("click", 3) not in desktop.calls
        clock["now"] = 200.0
        window = _inspect(client)["observation"]["windows"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "focus_window", "window_id": window["window_id"]},
        ).json()
        assert client.post("/api/desktop/control/permission", json={"enabled": False}).status_code == 200
        blocked = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert blocked["text"] == "Desktop control is not enabled."
        assert ("focus", 1) not in desktop.calls
        _enable(client)
        allowed = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert allowed["performed"] is True
        assert ("focus", 1) in desktop.calls


def test_invalid_targets_secrets_and_unlisted_actions_never_run(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    secret = "sk-" + "abcdefghij"
    with TestClient(app) as client:
        _enable(client)
        assert client.post("/api/desktop/control/preview", json={"action": "observe", "x": 1}).status_code == 422
        assert client.post("/api/desktop/control/preview", json={"action": "open_application"}).json()["text"] == "Opening an application uses its own approved action."
        assert client.post("/api/desktop/control/preview", json={"action": "shell"}).json()["text"] == "That desktop action is not allowed."
        assert client.post("/api/desktop/control/preview", json={"action": "type_text", "text": secret}).json()["text"] == "That desktop action is not supported."
        assert secret not in (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
        desktop.scene["title"] = "Windows Security"
        protected = _approve_observe(client)
        assert protected["text"] == "No Notepad window was found."
        assert protected["observation"] is None
        assert protected["performed"] is False
        assert "Windows Security" not in json.dumps(protected)
        desktop.scene["title"] = "Untitled - Notepad"
        desktop.scene["image"] = "consent.exe"
        refused = _approve_observe(client)
        assert refused["observation"] is None
        desktop.scene["image"] = "notepad.exe"
        desktop.scene["elements"][0]["password"] = True
        inspected = _inspect(client)
        roles = [element["role"] for element in inspected["observation"]["windows"][0]["elements"]]
        assert "edit" not in roles
        assert all(call == "observe" or call[0] != "type" for call in desktop.calls)
        window = inspected["observation"]["windows"][0]
        shortcut = client.post(
            "/api/desktop/control/preview",
            json={"action": "key_shortcut", "window_id": window["window_id"], "element_id": window["elements"][0]["element_id"], "shortcut": "ctrl+c"},
        ).json()
        assert shortcut["pending"] is None
        assert ("shortcut", 3) not in desktop.calls


def test_a_background_notepad_can_be_selected_and_is_rechecked(tmp_path: Path):
    desktop = FakeDesktop()
    desktop.others.append(
        {
            "handle": 4,
            "pid": 11,
            "image": "notepad.exe",
            "class_name": "Notepad",
            "title": "Notes - Notepad",
            "protected": False,
            "elements": [
                {"handle": 5, "role": "edit", "name": "Text editor", "password": False, "class_name": "Edit"},
            ],
        }
    )
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        inspected = _inspect(client)
        titles = [row["title"] for row in inspected["observation"]["windows"]]
        assert titles == ["Untitled - Notepad", "Notes - Notepad"]
        assert "Secret bank password" not in json.dumps(inspected)
        chosen = inspected["observation"]["windows"][1]
        editor = chosen["elements"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": chosen["window_id"], "element_id": editor["element_id"], "text": "Hello"},
        ).json()
        desktop.others[1]["pid"] = 12
        changed = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert changed["text"] == "The desktop changed before the action."
        assert ("type", 5, "Hello") not in desktop.calls
        desktop.others[1]["pid"] = 11
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": chosen["window_id"], "element_id": editor["element_id"], "text": "Hello"},
        ).json()
        desktop.others[1]["elements"][0]["handle"] = 8
        moved = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert moved["performed"] is False
        assert ("type", 5, "Hello") not in desktop.calls


def test_only_safe_notepad_windows_are_visible():
    rows = visible_notepad_windows(
        [
            {
                "handle": 4,
                "pid": 11,
                "image": "notepad.exe",
                "class_name": "Notepad",
                "title": "Notes - Notepad",
                "protected": False,
                "elements": [],
            },
            {
                "handle": 9,
                "pid": 99,
                "image": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Secret bank password",
                "protected": False,
                "elements": [],
            },
            {
                "handle": 7,
                "pid": 13,
                "image": "notepad.exe",
                "class_name": "Notepad",
                "title": "Password - Notepad",
                "protected": True,
                "elements": [],
            },
        ]
    )
    assert [row["title"] for row in rows] == ["Notes - Notepad"]
    assert "Secret bank password" not in json.dumps(rows)
    assert "Password - Notepad" not in json.dumps(rows)


def test_modern_tabbed_notepad_is_listed_and_other_windows_stay_hidden(tmp_path: Path):
    desktop = FakeDesktop()
    desktop.scene["title"] = "*Notes one, More tab - Notepad"
    desktop.scene["elements"] = [
        {"handle": 2, "role": "edit", "name": "Text editor", "password": False, "class_name": "RichEditD2DPT"},
    ]
    desktop.others.append(
        {
            "handle": 7,
            "pid": 13,
            "image": "notepad.exe",
            "class_name": "Notepad",
            "title": "Password - Notepad",
            "protected": True,
            "elements": [],
        }
    )
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        inspected = _inspect(client)
        windows = inspected["observation"]["windows"]
        assert [row["title"] for row in windows] == ["*Notes one, More tab - Notepad"]
        assert windows[0]["elements"][0]["role"] == "edit"
        body = json.dumps(inspected)
        assert "Secret bank password" not in body
        assert "Password - Notepad" not in body
        desktop.scene["title"] = "Notes * draft - Notepad"
        rejected = _approve_observe(client)
        assert rejected["text"] == "No Notepad window was found."
        assert "Notes * draft - Notepad" not in json.dumps(rejected)


def test_the_windows_11_editor_is_found_after_unrelated_child_windows():
    bridges = [
        {"handle": 100 + index, "class_name": "Microsoft.UI.Content.DesktopChildSiteBridge", "password": False, "notepad_owned": False, "name": ""}
        for index in range(20)
    ]
    children = bridges + [
        {"handle": 6, "class_name": "RichEditD2DPT", "password": False, "notepad_owned": True, "name": ""},
        {"handle": 8, "class_name": "RichEditD2DPT", "password": False, "notepad_owned": False, "name": "Other app"},
    ]
    rows = _targets_from_children(children)
    assert [(row["handle"], row["class_name"], row["role"]) for row in rows] == [(6, "RichEditD2DPT", "edit")]
    classic = _targets_from_children(
        [
            {"handle": 2, "class_name": "Edit", "password": False, "notepad_owned": True, "name": ""},
            {"handle": 3, "class_name": "Button", "password": True, "notepad_owned": True, "name": ""},
        ]
    )
    assert [row["class_name"] for row in classic] == ["Edit"]


def test_select_all_is_target_bound_and_a_second_preview_is_refused(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        editor = window["elements"][0]
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "key_shortcut", "window_id": window["window_id"], "element_id": editor["element_id"], "shortcut": "ctrl+a"},
        ).json()
        second = client.post("/api/desktop/control/preview", json={"action": "observe"}).json()
        assert second["text"] == "A desktop action is already waiting."
        assert desktop.calls == ["observe"]
        done = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert done["text"] == "The editor text was selected."
        assert ("shortcut", 2) in desktop.calls
        duplicate = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert duplicate["performed"] is False
        assert desktop.calls.count(("shortcut", 2)) == 1


def test_desktop_control_stays_separate_and_input_requires_authorization(tmp_path: Path):
    desktop = FakeDesktop()
    launches: list = []
    app = _app(tmp_path, desktop, launches)
    with TestClient(app) as client:
        page = client.get("/").text
        simulator = page.split('id="desktop-simulator"', 1)[1].split("</section>", 1)[0]
        notepad = page.split('id="notepad-action"', 1)[1].split("</section>", 1)[0]
        section = page.split('id="desktop-control"', 1)[1].split("</section>", 1)[0]
        assert "Desktop Control" in section
        assert "Preview action" in section
        assert "Inspect Notepad windows" in section
        assert "does not need to be the foreground window" in section
        assert "Desktop control is off." in section
        assert "id=\"desktop-control\"" not in simulator
        assert "id=\"desktop-control\"" not in notepad
        assert "Execute" not in section
        goal = client.post("/api/agent/goal", json={"task": "List the workspace."})
        assert goal.status_code == 200
        assert desktop.calls == []
    assert launches == []
    assert _REAL_FOCUS(1, lambda: False) is False
    assert _REAL_TYPE(1, "Hello", lambda: False) is False
    assert _REAL_TYPE(1, "hello\n", lambda: True) is False
    assert _REAL_CLICK(1, lambda: False) is False
    assert _REAL_SELECT(1, lambda: False) is False
    source = Path("jarvis/desktop/windows.py").read_text(encoding="utf-8")
    control = Path("jarvis/desktop/control.py").read_text(encoding="utf-8")
    for name in (source, control):
        assert "import subprocess" not in name
        assert "SendInput" not in name
        assert "SetCursorPos" not in name
        assert "mouse_event" not in name
        assert "notepad.py" not in name
    assert "jarvis.agent" not in control
    assert "jarvis.desktop.simulator" not in control
    assert "jarvis.desktop.notepad" not in control


class _FakeWin32:
    """Stand-in for user32 and kernel32. It never calls the real desktop."""

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


def test_mocked_windows_apis_discover_notepad_and_reject_other_targets(monkeypatch):
    specs = {
        1: {
            "top": True,
            "class": "Notepad",
            "pid": 10,
            "text": "*Notes one, More tab - Notepad",
            "children": [20, 21, 3, 4, 5],
            "root": 1,
        },
        20: {"class": "Microsoft.UI.Content.DesktopChildSiteBridge", "pid": 10, "root": 1, "text": ""},
        21: {"class": "InputSiteWindowClass", "pid": 10, "root": 1, "text": ""},
        3: {"class": "RichEditD2DPT", "pid": 10, "root": 1, "text": "Quarterly revenue figures"},
        4: {"class": "Button", "pid": 10, "root": 1, "text": "Save"},
        5: {"class": "Edit", "pid": 10, "root": 1, "style": 0x20, "text": "hunter2"},
        7: {"top": True, "class": "GDI+ Hook Window Class", "pid": 10, "text": "GDI+ Window (Notepad.exe)", "root": 7},
        8: {"top": True, "class": "Notepad", "pid": 12, "text": "Sign in - Notepad", "root": 8, "children": []},
        9: {"top": True, "class": "Chrome_WidgetWin_1", "pid": 99, "text": "Secret bank password", "root": 9, "children": []},
        11: {"class": "RichEditD2DPT", "pid": 99, "root": 9, "text": "Other editor"},
    }
    paths = {
        10: r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad\Notepad\Notepad.exe",
        12: r"C:\Windows\System32\notepad.exe",
        99: r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    }
    api = _FakeWin32(specs, paths)
    monkeypatch.setattr(windows_module, "_libraries", lambda: (api, api))
    listed = _REAL_LIST()
    body = json.dumps(listed)
    assert [row["title"] for row in listed["windows"]] == ["*Notes one, More tab - Notepad"]
    described = [(row["class_name"], row["password"], row["name"]) for row in listed["windows"][0]["elements"]]
    assert ("RichEditD2DPT", False, "") in described
    assert ("Button", False, "Save") in described
    assert ("Edit", True, "") in described
    accepted = _accept_window(listed["windows"][0])
    assert accepted is not None
    _window, elements = accepted
    assert [(row["role"], row["name"], row["password"]) for row in elements] == [("edit", "Text editor", False), ("button", "Save", False)]
    for hidden in ("Secret bank password", "Sign in - Notepad", "Quarterly revenue figures", "hunter2", "Other editor"):
        assert hidden not in body
    assert _REAL_FOCUS(1, lambda: False) is False
    assert api.calls == []
    assert _REAL_FOCUS(9, lambda: True) is False
    assert _REAL_FOCUS(1, lambda: True) is True
    assert ("foreground", 1) in api.calls
    assert _REAL_TYPE(3, "Hi", lambda: True) is True
    assert ("message", 3, 0x0102, ord("H")) in api.calls
    assert ("message", 3, 0x0102, ord("i")) in api.calls
    assert _REAL_TYPE(5, "Hi", lambda: True) is False
    assert _REAL_TYPE(11, "Hi", lambda: True) is False
    assert _REAL_TYPE(3, "hello\n", lambda: True) is False
    assert _REAL_CLICK(4, lambda: True) is True
    assert ("message", 4, 0x00F5, 0) in api.calls
    assert _REAL_CLICK(3, lambda: True) is False
    assert _REAL_SELECT(3, lambda: True) is True
    assert ("message", 3, 0x00B1, 0) in api.calls
    assert _REAL_SELECT(4, lambda: True) is False
    sent = {"count": 0}

    def allow_once() -> bool:
        sent["count"] += 1
        return sent["count"] < 4

    assert _REAL_TYPE(3, "AB", allow_once) is True
    assert [call for call in api.calls if call[:3] == ("message", 3, 0x0102) and call[3] in {ord("A"), ord("B")}] == [
        ("message", 3, 0x0102, ord("A"))
    ]
    assert ("foreground", 9) not in api.calls
    assert ("message", 5, 0x0102, ord("H")) not in api.calls
    assert ("message", 11, 0x0102, ord("H")) not in api.calls


def test_missing_forged_and_changed_targets_are_rejected(tmp_path: Path):
    desktop = FakeDesktop()
    desktop.others.append(
        {
            "handle": 4,
            "pid": 11,
            "image": "notepad.exe",
            "class_name": "Notepad",
            "title": "Notes - Notepad",
            "protected": False,
            "elements": [
                {"handle": 5, "role": "edit", "name": "Quarterly revenue figures", "password": False, "class_name": "RichEditD2DPT"},
            ],
        }
    )
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        found = desktop.others
        desktop.others = []
        desktop.scene["image"] = "explorer.exe"
        empty = _approve_observe(client)
        assert empty["text"] == "No Notepad window was found."
        assert empty["observation"] is None
        assert empty["performed"] is False
        desktop.scene["image"] = "notepad.exe"
        desktop.scene["title"] = "Sign in - Notepad"
        signed_in = _approve_observe(client)
        assert signed_in["text"] == "No Notepad window was found."
        assert "Sign in - Notepad" not in json.dumps(signed_in)
        desktop.scene["title"] = "Untitled - Notepad"
        desktop.others = found
        inspected = _inspect(client)
        windows = inspected["observation"]["windows"]
        assert [row["title"] for row in windows] == ["Untitled - Notepad", "Notes - Notepad"]
        assert windows[1]["elements"][0]["name"] == "Text editor"
        assert "Quarterly revenue figures" not in json.dumps(inspected)
        window = windows[0]
        other = windows[1]
        forged = "ab" * 16
        assert client.post(
            "/api/desktop/control/preview",
            json={"action": "focus_window", "window_id": forged},
        ).json()["text"] == "That desktop target is not allowed."
        assert client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": other["elements"][0]["element_id"], "text": "Hello"},
        ).json()["text"] == "That desktop target is not allowed."
        assert client.post(
            "/api/desktop/control/confirm",
            json={"action_id": "0123456789abcdef0123456789abcdef", "approved": True},
        ).json()["performed"] is False
        assert client.post("/api/desktop/control/confirm", json={"action_id": "nope", "approved": True}).status_code == 422
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": window["elements"][0]["element_id"], "text": "Hello"},
        ).json()
        desktop.scene["handle"] = 50
        moved = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert moved["text"] == "The desktop changed before the action."
        assert ("type", 2, "Hello") not in desktop.calls
        desktop.scene["handle"] = 1
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": window["elements"][0]["element_id"], "text": "Hello"},
        ).json()
        desktop.scene["image"] = "cmd.exe"
        changed = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert changed["performed"] is False
        assert ("type", 2, "Hello") not in desktop.calls
        desktop.scene["image"] = "notepad.exe"
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": window["elements"][0]["element_id"], "text": "Hello"},
        ).json()
        desktop.scene["elements"][0]["role"] = "button"
        swapped = client.post(
            "/api/desktop/control/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert swapped["text"] == "The desktop changed before the action."
        assert all(call == "observe" or call[0] != "type" for call in desktop.calls)


def test_concurrent_approval_cancel_and_stop_do_not_replay(tmp_path: Path):
    desktop = FakeDesktop()
    app = _app(tmp_path, desktop)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        editor = window["elements"][0]
        control = app.state.desktop_control
        preview = client.post(
            "/api/desktop/control/preview",
            json={"action": "type_text", "window_id": window["window_id"], "element_id": editor["element_id"], "text": "Hello"},
        ).json()
        action_id = preview["pending"]["action_id"]
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def confirm() -> None:
            barrier.wait(timeout=2)
            results.append(control.confirm(action_id, True))

        threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert desktop.calls.count(("type", 2, "Hello")) == 1
        assert sum(item["performed"] is True for item in results) == 1
        assert control.confirm(action_id, True)["performed"] is False
        assert desktop.calls.count(("type", 2, "Hello")) == 1

        typed = 1
        for _ in range(12):
            waiting = client.post(
                "/api/desktop/control/preview",
                json={"action": "type_text", "window_id": window["window_id"], "element_id": editor["element_id"], "text": "Hello"},
            ).json()
            current = waiting["pending"]["action_id"]
            gate = threading.Barrier(2)
            outcomes: list[tuple[str, dict]] = []

            def approve(action=current, arrived=gate, box=outcomes) -> None:
                arrived.wait(timeout=2)
                box.append(("confirm", control.confirm(action, True)))

            def cancel(arrived=gate, box=outcomes) -> None:
                arrived.wait(timeout=2)
                box.append(("cancel", control.cancel()))

            pair = [threading.Thread(target=approve), threading.Thread(target=cancel)]
            for thread in pair:
                thread.start()
            for thread in pair:
                thread.join(timeout=2)
            delta = desktop.calls.count(("type", 2, "Hello")) - typed
            assert delta in {0, 1}
            typed += delta
            assert control.confirm(current, True)["performed"] is False
        assert desktop.calls.count(("type", 2, "Hello")) == typed

    gated = FakeDesktop()
    entered = threading.Event()
    release = threading.Event()

    def focus(handle: int, allow) -> bool:
        if not allow():
            return False
        entered.set()
        assert release.wait(timeout=2)
        if not allow():
            return False
        gated.calls.append(("focus", handle))
        return True

    gated.focus = focus
    app = _app(tmp_path, gated)
    with TestClient(app) as client:
        _enable(client)
        window = _inspect(client)["observation"]["windows"][0]
        waiting = client.post("/api/desktop/control/preview", json={"action": "focus_window", "window_id": window["window_id"]}).json()
        action_id = waiting["pending"]["action_id"]
        result: dict = {}

        def run() -> None:
            result.update(app.state.desktop_control.confirm(action_id, True))

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(timeout=2)
        app.state.halt.emergency_stop()
        release.set()
        worker.join(timeout=2)
        app.state.desktop_control.observe_stop()
        assert ("focus", 1) not in gated.calls
        assert result["performed"] is False
        app.state.halt.resume()
        replay = app.state.desktop_control.confirm(action_id, True)
        assert replay["performed"] is False
        assert ("focus", 1) not in gated.calls
        assert app.state.desktop_control.preview({"action": "focus_window", "window_id": window["window_id"]})["text"] == "Inspect the window before choosing an action."
