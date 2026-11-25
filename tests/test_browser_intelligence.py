"""Browser Intelligence tests. These never launch or control a real browser."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.browser import windows as browser_windows
from jarvis.browser.windows import inspection_from_session, visible_browser_sessions
from jarvis.config import Settings
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.policy import Permission

_REAL_LIST = browser_windows.list_browser_sessions
_REAL_READ = browser_windows.read_browser_session


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list = []
        self.scene = {
            "handle": 11,
            "pid": 21,
            "image": "msedge.exe",
            "class_name": "Chrome_WidgetWin_1",
            "title": "Example Docs - Microsoft Edge",
            "browser": "Microsoft Edge",
            "protected": False,
        }
        self.others: list[dict] = [
            {
                "handle": 12,
                "pid": 22,
                "image": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Notes and 2 more pages - Google Chrome",
                "browser": "Google Chrome",
                "protected": False,
            },
            {
                "handle": 13,
                "pid": 23,
                "image": "firefox.exe",
                "class_name": "MozillaWindowClass",
                "title": "Secret bank password",
                "browser": "Firefox",
                "protected": False,
            },
            {
                "handle": 14,
                "pid": 24,
                "image": "msedge.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Sign in - Microsoft Edge",
                "browser": "Microsoft Edge",
                "protected": False,
            },
        ]

    def list_sessions(self) -> dict:
        self.calls.append("list")
        rows = [json.loads(json.dumps(self.scene)), *json.loads(json.dumps(self.others))]
        return {"sessions": visible_browser_sessions(rows)}

    def inspect_session(self, handle: int, pid: int, image: str, class_name: str, title: str) -> dict | None:
        self.calls.append(("inspect", handle, pid, image, class_name, title))
        if handle != self.scene["handle"] or pid != self.scene["pid"] or image != self.scene["image"]:
            return None
        if class_name != self.scene["class_name"] or title != self.scene["title"]:
            return None
        return inspection_from_session(self.scene)


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("tests must not touch a real browser")

    monkeypatch.setattr(browser_windows, "list_browser_sessions", boom)
    monkeypatch.setattr(browser_windows, "read_browser_session", boom)


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


def _app(tmp_path: Path, browser: FakeBrowser):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("browser must not launch apps")),
        calculator_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("browser must not launch apps")),
        browser_port=browser,
    )


def _enable(client: TestClient) -> None:
    response = client.post("/api/browser/permission", json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"permission": "browser", "enabled": True}


def _list(client: TestClient) -> dict:
    preview = client.post("/api/browser/preview", json={"action": "list"}).json()
    assert preview["pending"]["action"] == "list"
    done = client.post(
        "/api/browser/confirm",
        json={"action_id": preview["pending"]["action_id"], "approved": True},
    ).json()
    body = json.dumps(done)
    assert "Secret bank password" not in body
    assert "Sign in - Microsoft Edge" not in body
    assert "handle" not in body
    return done


def test_permission_off_and_inspection_needs_approval(tmp_path: Path):
    browser = FakeBrowser()
    app = _app(tmp_path, browser)
    with TestClient(app) as client:
        state = client.get("/api/browser").json()
        assert state["permission_enabled"] is False
        refused = client.post("/api/browser/preview", json={"action": "list"}).json()
        assert refused["text"] == "Browser intelligence is not enabled."
        assert browser.calls == []
        _enable(client)
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["automate"] is False
        assert permissions["app_control"] is False
        assert app.state.policy.enabled[Permission.AUTOMATE] is False
        listed = _list(client)
        assert listed["performed"] is True
        titles = [row["title"] for row in listed["sessions"]["sessions"]]
        assert titles == ["Example Docs - Microsoft Edge", "Notes and 2 more pages - Google Chrome"]
        session = listed["sessions"]["sessions"][0]
        preview = client.post(
            "/api/browser/preview",
            json={"action": "inspect", "session_id": session["session_id"]},
        ).json()
        assert preview["pending"]["summary"] == "Inspect Microsoft Edge."
        assert ("inspect", 11, 21, "msedge.exe", "Chrome_WidgetWin_1", "Example Docs - Microsoft Edge") not in browser.calls
        inspected = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert inspected["inspection"]["browser"] == "Microsoft Edge"
        assert inspected["inspection"]["tab_title"] == "Example Docs"
        assert inspected["inspection"]["tab_count"] == 1
        assert inspected["inspection"]["url"] == ""
        assert inspected["inspection"]["url_available"] is False
        assert "Example Docs" not in (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
        cleared = client.post("/api/browser/clear", json={}).json()
        assert cleared["inspection"] is None
        assert cleared["sessions"] is None
    assert EXECUTION_ENABLED is False


def test_denial_cancel_expiry_stop_and_resume(tmp_path: Path):
    browser = FakeBrowser()
    app = _app(tmp_path, browser)
    clock = {"now": 0.0}
    app.state.browser_intelligence._clock = lambda: clock["now"]
    with TestClient(app) as client:
        _enable(client)
        preview = client.post("/api/browser/preview", json={"action": "list"}).json()
        denied = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["text"] == "The action was denied."
        assert browser.calls == []
        preview = client.post("/api/browser/preview", json={"action": "list"}).json()
        cancelled = client.post("/api/browser/cancel", json={}).json()
        assert cancelled["text"] == "The action was cancelled."
        assert client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        preview = client.post("/api/browser/preview", json={"action": "list"}).json()
        clock["now"] = 120.0
        expired = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert expired["text"] == "That approval has expired."
        assert browser.calls == []
        clock["now"] = 200.0
        listed = _list(client)
        session = listed["sessions"]["sessions"][0]
        preview = client.post(
            "/api/browser/preview",
            json={"action": "inspect", "session_id": session["session_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        stale = client.post("/api/browser/confirm", json={"action_id": action_id, "approved": True}).json()
        assert stale["performed"] is False
        assert client.get("/api/browser").json()["inspection"] is None
        assert client.get("/api/browser").json()["sessions"] is None


def test_forged_duplicate_changed_and_malformed_targets(tmp_path: Path):
    browser = FakeBrowser()
    app = _app(tmp_path, browser)
    with TestClient(app) as client:
        _enable(client)
        assert client.post("/api/browser/preview", json={"action": "list", "x": 1}).status_code == 422
        assert client.post("/api/browser/preview", json={"action": "navigate"}).json()["text"] == "That browser action is not allowed."
        listed = _list(client)
        forged = "ab" * 16
        assert client.post(
            "/api/browser/preview",
            json={"action": "inspect", "session_id": forged},
        ).json()["text"] == "That browser session is not available."
        assert client.post(
            "/api/browser/confirm",
            json={"action_id": "0123456789abcdef0123456789abcdef", "approved": True},
        ).json()["performed"] is False
        assert client.post("/api/browser/confirm", json={"action_id": "short", "approved": True}).status_code == 422
        session = listed["sessions"]["sessions"][0]
        preview = client.post(
            "/api/browser/preview",
            json={"action": "inspect", "session_id": session["session_id"]},
        ).json()
        browser.scene["title"] = "Changed Docs - Microsoft Edge"
        changed = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert changed["text"] == "The browser changed before it could be inspected."
        assert changed["inspection"] is None
        browser.scene["title"] = "Example Docs - Microsoft Edge"
        preview = client.post(
            "/api/browser/preview",
            json={"action": "inspect", "session_id": session["session_id"]},
        ).json()
        done = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert done["performed"] is True
        duplicate = client.post(
            "/api/browser/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert duplicate["performed"] is False


def test_concurrent_list_approvals_run_at_most_once(tmp_path: Path):
    browser = FakeBrowser()
    app = _app(tmp_path, browser)
    with TestClient(app) as client:
        _enable(client)
        preview = client.post("/api/browser/preview", json={"action": "list"}).json()
        action_id = preview["pending"]["action_id"]
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def confirm() -> None:
            barrier.wait(timeout=2)
            results.append(app.state.browser_intelligence.confirm(action_id, True))

        threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert browser.calls.count("list") == 1
        assert sum(item.get("performed") is True for item in results) == 1
        assert app.state.browser_intelligence.confirm(action_id, True)["performed"] is False


def test_oversized_secret_and_unexpected_identities_are_rejected():
    rows = visible_browser_sessions(
        [
            {
                "handle": 1,
                "pid": 2,
                "image": "msedge.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": "Ok - Microsoft Edge",
                "browser": "Microsoft Edge",
                "protected": False,
            },
            {
                "handle": 3,
                "pid": 4,
                "image": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
                "title": ("A" * 200) + " - Google Chrome",
                "browser": "Google Chrome",
                "protected": False,
            },
            {
                "handle": 5,
                "pid": 6,
                "image": "firefox.exe",
                "class_name": "MozillaWindowClass",
                "title": "Secret bank password",
                "browser": "Firefox",
                "protected": False,
            },
        ]
    )
    assert [row["title"] for row in rows] == ["Ok - Microsoft Edge"]
    assert "Secret bank password" not in json.dumps(rows)
    huge = inspection_from_session(
        {
            "handle": 1,
            "pid": 2,
            "image": "msedge.exe",
            "class_name": "Chrome_WidgetWin_1",
            "title": ("B" * 200) + " - Microsoft Edge",
            "browser": "Microsoft Edge",
            "protected": False,
        }
    )
    assert huge is None
    secret = inspection_from_session(
        {
            "handle": 1,
            "pid": 2,
            "image": "chrome.exe",
            "class_name": "Chrome_WidgetWin_1",
            "title": "sk-abcdefghijklmnop - Google Chrome",
            "browser": "Google Chrome",
            "protected": False,
        }
    )
    # Protected/login markers and secret shapes are blocked at accept time in the controller.
    assert secret is not None
    assert secret["url"] == ""
    multi = inspection_from_session(
        {
            "handle": 12,
            "pid": 22,
            "image": "chrome.exe",
            "class_name": "Chrome_WidgetWin_1",
            "title": "Notes and 2 more pages - Google Chrome",
            "browser": "Google Chrome",
            "protected": False,
        }
    )
    assert multi == {
        "browser": "Google Chrome",
        "image": "chrome.exe",
        "tab_title": "Notes",
        "url": "",
        "url_available": False,
        "tab_count": 3,
        "window_title": "Notes and 2 more pages - Google Chrome",
    }


def test_provider_goal_and_desktop_modules_stay_disconnected(tmp_path: Path):
    browser = FakeBrowser()
    app = _app(tmp_path, browser)
    with TestClient(app) as client:
        page = client.get("/").text
        section = page.split('id="browser-intel"', 1)[1].split("</section>", 1)[0]
        engine = page.split('id="desktop-engine"', 1)[1].split("</section>", 1)[0]
        assert "Browser Intelligence" in section
        assert "Inspect session" in section
        assert "does not open pages" in section
        assert "Execute" not in section
        assert "id=\"browser-intel\"" not in engine
        _enable(client)
        goal = client.post("/api/agent/goal", json={"task": "List the workspace."})
        assert goal.status_code == 200
        chat = client.post("/api/chat", json={"text": "Inspect my browser tabs"})
        assert chat.status_code == 200
        assert browser.calls == []
        memories = client.get("/api/memories").json()
        assert "Example Docs" not in json.dumps(memories)
    source = Path("jarvis/browser/intelligence.py").read_text(encoding="utf-8")
    windows = Path("jarvis/browser/windows.py").read_text(encoding="utf-8")
    for text in (source, windows):
        for banned in (
            "import subprocess",
            "SendInput",
            "SetCursorPos",
            "mouse_event",
            "9222",
            "websocket",
            "jarvis.desktop.control",
            "jarvis.desktop.engine",
            "jarvis.desktop.simulator",
            "jarvis.agent",
            "Permission",
        ):
            assert banned not in text
    assert _REAL_LIST is not None
    assert _REAL_READ is not None
