"""Browser Bridge tests. These never launch or control a real browser."""

from __future__ import annotations

import json
import struct
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.browser import bridge_host
from jarvis.browser.bridge import BrowserBridge
from jarvis.config import Settings
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission
from jarvis.audit import AuditLog


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


def _app(tmp_path: Path):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bridge must not launch apps")),
        calculator_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bridge must not launch apps")),
        browser_port=object(),
    )


def _enable(client: TestClient, app) -> str:
    response = client.post("/api/browser/bridge/permission", json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"permission": "browser_bridge", "enabled": True}
    token = app.state.browser_bridge._bridge_token
    assert len(token) >= 32
    return token


def _handshake(client: TestClient, token: str, pair_code: str, *, browser: str = "Google Chrome") -> dict:
    return client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {
                "type": "handshake",
                "pair_code": pair_code,
                "browser": browser,
                "extension_version": "1.2.0",
                "protocol_version": "1.2.0",
            },
            "raw_size": 120,
        },
    ).json()


def _inspect(client: TestClient, token: str, session_id: str, *, title: str = "Example Docs", url: str = "https://example.com/docs") -> dict:
    return client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {
                "type": "inspect",
                "session_id": session_id,
                "browser": "Google Chrome",
                "tab_title": title,
                "tab_url": url,
            },
            "raw_size": 160,
        },
    ).json()


def test_permission_off_blocks_ingress_and_defaults_off(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        state = client.get("/api/browser/bridge").json()
        assert state["permission_enabled"] is False
        assert state["pair_code"] == ""
        assert state["session"] is None
        assert state["inspection"] is None
        refused = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": "x" * 40},
            json={"message": {"type": "ping", "session_id": "ab" * 16}, "raw_size": 40},
        ).json()
        assert refused == {"ok": False, "error": "disabled"}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["automate"] is False
        assert permissions["app_control"] is False
        assert app.state.policy.enabled[Permission.AUTOMATE] is False
    assert EXECUTION_ENABLED is False


def test_handshake_identity_and_inspect_approval(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        state = client.get("/api/browser/bridge").json()
        pair = state["pair_code"]
        assert len(pair) == 8
        bad_browser = _handshake(client, token, pair, browser="Firefox")
        assert bad_browser == {"ok": False, "error": "browser"}
        wrong_pair = _handshake(client, token, "AAAAAAAA")
        assert wrong_pair == {"ok": False, "error": "pairing"}
        ok = _handshake(client, token, pair)
        assert ok["ok"] is True
        assert ok["type"] == "handshake_ok"
        assert ok["protocol_version"] == "1.2.0"
        assert ok["capabilities"] == ["inspect_active_tab", "read_active_tab", "browser_actions"]
        session_id = ok["session_id"]
        connected = client.get("/api/browser/bridge").json()
        assert connected["session"]["browser"] == "Google Chrome"
        assert connected["pair_code"] == ""
        pending = _inspect(client, token, session_id)
        assert pending["ok"] is True
        assert pending["type"] == "inspect_pending"
        preview = client.get("/api/browser/bridge").json()
        assert preview["pending"]["tab_title"] == "Example Docs"
        assert preview["pending"]["tab_url"] == "https://example.com/docs"
        assert preview["inspection"] is None
        denied = client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["inspection"] is None
        assert denied["text"] == "The action was denied."
        pending = _inspect(client, token, session_id)
        approved = client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": pending["action_id"], "approved": True},
        ).json()
        assert approved["inspection"]["tab_title"] == "Example Docs"
        assert approved["inspection"]["tab_url"] == "https://example.com/docs"
        assert approved["performed"] is True
        cleared = client.post("/api/browser/bridge/clear", json={}).json()
        assert cleared["inspection"] is None


def test_invalid_forged_size_rate_and_malformed(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        assert client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": "not-the-bridge-token-" + ("a" * 20)},
            json={"message": {"type": "handshake", "pair_code": pair, "browser": "Google Chrome", "extension_version": "1.2.0", "protocol_version": "1.2.0"}, "raw_size": 80},
        ).json() == {"ok": False, "error": "unauthorized"}
        assert client.post(
            "/api/browser/bridge/ingress",
            json={"message": {"type": "handshake", "pair_code": pair, "browser": "Google Chrome", "extension_version": "1.2.0", "protocol_version": "1.2.0"}, "raw_size": 80},
        ).status_code == 401
        too_large = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={"message": {"type": "ping", "session_id": "ab" * 16}, "raw_size": 20000},
        )
        assert too_large.status_code == 422
        forged = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={
                "message": {
                    "type": "handshake",
                    "pair_code": pair,
                    "browser": "Google Chrome",
                    "extension_version": "1.2.0",
                    "protocol_version": "1.2.0",
                    "extra": True,
                },
                "raw_size": 100,
            },
        ).json()
        assert forged == {"ok": False, "error": "invalid"}
        ok = _handshake(client, token, pair)
        session_id = ok["session_id"]
        bad_url = _inspect(client, token, session_id, url="javascript:alert(1)")
        assert bad_url == {"ok": False, "error": "url"}
        bad_title = _inspect(client, token, session_id, title="<script>x</script>")
        assert bad_title == {"ok": False, "error": "title"}
        stale = _inspect(client, token, "cd" * 16)
        assert stale == {"ok": False, "error": "session"}
        app.state.browser_bridge._ingress_times.clear()
        for _ in range(20):
            reply = client.post(
                "/api/browser/bridge/ingress",
                headers={"X-Jarvis-Bridge-Token": token},
                json={"message": {"type": "ping", "session_id": session_id}, "raw_size": 40},
            ).json()
            assert reply["ok"] is True
        limited = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={"message": {"type": "ping", "session_id": session_id}, "raw_size": 40},
        ).json()
        assert limited == {"ok": False, "error": "rate_limited"}


def test_cancel_expiry_disconnect_stop_resume_no_replay(tmp_path: Path):
    app = _app(tmp_path)
    clock = {"now": 0.0}
    app.state.browser_bridge._clock = lambda: clock["now"]
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        pending = _inspect(client, token, session_id)
        cancelled = client.post("/api/browser/bridge/cancel", json={}).json()
        assert cancelled["pending"] is None
        assert client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": pending["action_id"], "approved": True},
        ).json()["performed"] is False
        pending = _inspect(client, token, session_id)
        clock["now"] = 120.0
        expired = client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": pending["action_id"], "approved": True},
        ).json()
        assert expired["text"] == "That approval has expired."
        clock["now"] = 200.0
        client.post("/api/browser/bridge/pair", json={})
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        pending = _inspect(client, token, session_id)
        action_id = pending["action_id"]
        assert client.post("/api/browser/bridge/disconnect", json={}).json()["session"] is None
        assert client.get("/api/browser/bridge").json()["inspection"] is None
        client.post("/api/browser/bridge/pair", json={})
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        pending = _inspect(client, token, session_id, title="After reconnect")
        action_id = pending["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        stopped = client.get("/api/browser/bridge").json()
        assert stopped["session"] is None
        assert stopped["pending"] is None
        assert stopped["inspection"] is None
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        resumed = client.get("/api/browser/bridge").json()
        assert resumed["session"] is None
        assert resumed["pending"] is None
        assert resumed["inspection"] is None
        stale = client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": action_id, "approved": True},
        ).json()
        assert stale["performed"] is False


def test_concurrent_inspect_is_busy(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        first = _inspect(client, token, session_id)
        assert first["ok"] is True
        second = _inspect(client, token, session_id, title="Other Tab")
        assert second == {"ok": False, "error": "busy"}


def test_token_and_inspection_do_not_leak(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        pending = _inspect(client, token, session_id, title="Private Notes", url="https://secret.example/path?q=1")
        approved = client.post(
            "/api/browser/bridge/confirm",
            json={"action_id": pending["action_id"], "approved": True},
        ).json()
        assert approved["inspection"]["tab_url"] == "https://secret.example/path?q=1"
        status = client.get("/api/status").json()
        blob = json.dumps(status) + json.dumps(client.get("/api/messages").json()) + json.dumps(client.get("/api/memories").json())
        assert token not in blob
        assert "bridge_token" not in blob
        assert "Private Notes" not in blob
        assert "secret.example" not in blob
        audit = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
        assert token not in audit
        assert "Private Notes" not in audit
        assert "secret.example" not in audit
        assert "bridge_token" not in audit
        bridge_view = client.get("/api/browser/bridge").json()
        assert "bridge_token" not in json.dumps(bridge_view)
        assert token not in json.dumps(bridge_view)
        config = (tmp_path / "data" / "bridge_native.json").read_text(encoding="utf-8")
        assert token in config
        client.post("/api/browser/bridge/permission", json={"enabled": False})
        assert not (tmp_path / "data" / "bridge_native.json").exists()


def test_incompatible_protocol_and_ping_disconnect(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        bad = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={
                "message": {
                    "type": "handshake",
                    "pair_code": pair,
                    "browser": "Microsoft Edge",
                    "extension_version": "1.2.0",
                    "protocol_version": "9.9.9",
                },
                "raw_size": 100,
            },
        ).json()
        assert bad == {"ok": False, "error": "incompatible"}
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair, browser="Microsoft Edge")["session_id"]
        pong = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={"message": {"type": "ping", "session_id": session_id}, "raw_size": 40},
        ).json()
        assert pong == {"ok": True, "type": "pong", "session_id": session_id}
        gone = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={"message": {"type": "disconnect", "session_id": session_id}, "raw_size": 40},
        ).json()
        assert gone == {"ok": True, "type": "disconnected"}
        assert client.get("/api/browser/bridge").json()["session"] is None


def test_bridge_isolated_from_desktop_and_goal(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _enable(client, app)
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)["session_id"]
        pending = _inspect(client, token, session_id)
        client.post("/api/browser/bridge/confirm", json={"action_id": pending["action_id"], "approved": True})
        assert client.get("/api/desktop/engine").json()["permission_enabled"] is False
        goal = client.get("/api/agent/goal").json()
        assert "Example Docs" not in json.dumps(goal)
        assert "example.com" not in json.dumps(goal)


def test_native_host_forwards_with_mock_http(tmp_path: Path, monkeypatch):
    import urllib.request as ureq

    config_path = tmp_path / "data" / "bridge_native.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"host": "127.0.0.1", "port": 8765, "bridge_token": "t" * 40, "native_host": "com.jarvis.browser_bridge"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_host, "_project_root", lambda: tmp_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _n=None):
            return b'{"ok":true,"type":"pong","session_id":"' + b"ab" * 16 + b'"}'

    def fake_urlopen(request, timeout=5):
        captured["url"] = request.full_url
        headers = {str(key).lower(): value for key, value in request.header_items()}
        captured["token"] = headers.get("x-jarvis-bridge-token")
        captured["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(ureq, "urlopen", fake_urlopen)
    payload = {"type": "ping", "session_id": "ab" * 16}
    raw = json.dumps(payload).encode("utf-8")
    stdin = struct.pack("<I", len(raw)) + raw

    class _Std:
        def __init__(self, buf: _BytesIO) -> None:
            self.buffer = buf

    out = _BytesIO()
    monkeypatch.setattr(bridge_host.sys, "stdin", _Std(_BytesIO(stdin)))
    monkeypatch.setattr(bridge_host.sys, "stdout", _Std(out))
    assert bridge_host.main() == 0
    assert captured["url"].endswith("/api/browser/bridge/ingress")
    assert captured["token"] == "t" * 40
    length = struct.unpack("<I", out.getvalue()[:4])[0]
    reply = json.loads(out.getvalue()[4 : 4 + length])
    assert reply["ok"] is True
    assert reply["type"] == "pong"


class _BytesIO:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self._pos = 0
        self._out = bytearray()

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> int:
        self._out.extend(data)
        return len(data)

    def flush(self) -> None:
        return None

    def getvalue(self) -> bytes:
        return bytes(self._out)


def test_unit_bridge_rejects_secretish_fields(tmp_path: Path):
    halt = HaltController()
    bridge = BrowserBridge(halt, AuditLog(tmp_path / "audit.jsonl"), config_dir=tmp_path, port=8765)
    bridge.set_enabled(True)
    token = bridge._bridge_token
    pair = bridge.status()["pair_code"]
    session = bridge.ingress(
        token,
        {
            "type": "handshake",
            "pair_code": pair,
            "browser": "Google Chrome",
            "extension_version": "1.2.0",
            "protocol_version": "1.2.0",
        },
        raw_size=80,
    )
    rejected = bridge.ingress(
        token,
        {
            "type": "inspect",
            "session_id": session["session_id"],
            "browser": "Google Chrome",
            "tab_title": "sk-abcdefghijklmnop",
            "tab_url": "https://example.com",
        },
        raw_size=80,
    )
    assert rejected == {"ok": False, "error": "rejected"}


def test_threads_do_not_double_approve(tmp_path: Path):
    halt = HaltController()
    bridge = BrowserBridge(halt, AuditLog(tmp_path / "audit.jsonl"), config_dir=tmp_path, port=8765)
    bridge.set_enabled(True)
    token = bridge._bridge_token
    pair = bridge.status()["pair_code"]
    session = bridge.ingress(
        token,
        {
            "type": "handshake",
            "pair_code": pair,
            "browser": "Google Chrome",
            "extension_version": "1.2.0",
            "protocol_version": "1.2.0",
        },
        raw_size=80,
    )
    pending = bridge.ingress(
        token,
        {
            "type": "inspect",
            "session_id": session["session_id"],
            "browser": "Google Chrome",
            "tab_title": "Example Docs",
            "tab_url": "https://example.com/docs",
        },
        raw_size=80,
    )
    action_id = pending["action_id"]
    results: list[dict] = []

    def decide() -> None:
        results.append(bridge.confirm(action_id, True))

    threads = [threading.Thread(target=decide) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    performed = [row for row in results if row.get("performed")]
    assert len(performed) == 1
    assert bridge.status()["inspection"]["tab_title"] == "Example Docs"
