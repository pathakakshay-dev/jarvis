"""Browser Actions tests. These never launch or control a real browser."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.desktop.safety import EXECUTION_ENABLED
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.policy import Permission

_BUTTON = {
    "element_id": "aaaaaaaaaaaaaaaa",
    "kind": "button",
    "tag": "button",
    "input_type": "",
    "label": "Save Draft",
    "fingerprint": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}
_FIELD = {
    "element_id": "cccccccccccccccc",
    "kind": "text_field",
    "tag": "input",
    "input_type": "text",
    "label": "Notes Field",
    "fingerprint": "dddddddddddddddddddddddddddddddd",
}


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
        fake=fake or FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("actions must not launch apps")),
        calculator_launcher=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("actions must not launch apps")),
        browser_port=object(),
    )


def _enable_bridge(client: TestClient, app) -> tuple[str, str]:
    assert client.post("/api/browser/bridge/permission", json={"enabled": True}).json()["enabled"] is True
    token = app.state.browser_bridge._bridge_token
    pair = client.get("/api/browser/bridge").json()["pair_code"]
    return token, pair


def _handshake(client: TestClient, token: str, pair: str) -> str:
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
    assert "browser_actions" in reply["capabilities"]
    return reply["session_id"]


def _scan(client: TestClient, token: str, session_id: str, elements: list[dict], *, title="Example Docs", url="https://example.com/docs") -> dict:
    return client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {
                "type": "action_targets",
                "session_id": session_id,
                "browser": "Google Chrome",
                "tab_title": title,
                "tab_url": url,
                "elements": elements,
            },
            "raw_size": 400,
        },
    ).json()


def _claim(client: TestClient, token: str, session_id: str) -> dict:
    return client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {"type": "action_claim", "session_id": session_id, "browser": "Google Chrome"},
            "raw_size": 80,
        },
    ).json()


def _result(
    client: TestClient,
    token: str,
    session_id: str,
    job: dict,
    *,
    outcome: str = "completed",
    verified: bool = True,
    title: str | None = None,
    url: str | None = None,
    element_id: str | None = None,
    fingerprint: str | None = None,
) -> dict:
    return client.post(
        "/api/browser/bridge/ingress",
        headers={"X-Jarvis-Bridge-Token": token},
        json={
            "message": {
                "type": "action_result",
                "session_id": session_id,
                "browser": "Google Chrome",
                "action_id": job["action_id"],
                "element_id": element_id or job["element_id"],
                "fingerprint": fingerprint or job["fingerprint"],
                "tab_title": title if title is not None else job["tab_title"],
                "tab_url": url if url is not None else job["tab_url"],
                "outcome": outcome,
                "verified": verified,
            },
            "raw_size": 200,
        },
    ).json()


def test_permission_disabled(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        state = client.get("/api/browser/bridge/actions").json()
        assert state["permission_enabled"] is False
        token, pair = _enable_bridge(client, app)
        session_id = _handshake(client, token, pair)
        refused = _scan(client, token, session_id, [_BUTTON])
        assert refused == {"ok": False, "error": "actions_disabled"}
        assert client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()["text"] == "Browser actions are not enabled."
        assert app.state.policy.enabled[Permission.AUTOMATE] is False
    assert EXECUTION_ENABLED is False


def test_approval_denial_and_click_flow(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        assert client.post("/api/browser/bridge/actions/permission", json={"enabled": True}).json()["enabled"] is True
        session_id = _handshake(client, token, pair)
        assert _scan(client, token, session_id, [_BUTTON, _FIELD])["ok"] is True
        denied_preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        assert denied_preview["pending"]["summary"] == "Click Save Draft."
        denied = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": denied_preview["pending"]["action_id"], "approved": False},
        ).json()
        assert denied["ready"] is None
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        approved = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert approved["ready"]["action"] == "click"
        job = _claim(client, token, session_id)
        assert job["type"] == "action_job"
        assert "text" not in job
        done = _result(client, token, session_id, job)
        assert done == {"ok": True, "type": "action_done"}
        assert client.get("/api/browser/bridge/actions").json()["result"]["outcome"] == "completed"


def test_type_requires_separate_approval_and_hides_from_audit(tmp_path: Path):
    fake = FakeProvider()
    app = _app(tmp_path, fake)
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        session_id = _handshake(client, token, pair)
        _scan(client, token, session_id, [_FIELD])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "type", "element_id": _FIELD["element_id"], "text": "Hello notes"},
        ).json()
        assert preview["pending"]["text"] == "Hello notes"
        approved = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert approved["ready"] is not None
        job = _claim(client, token, session_id)
        assert job["text"] == "Hello notes"
        _result(client, token, session_id, job)
        audit = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
        assert "Hello notes" not in audit
        assert fake.calls == 0
        blob = json.dumps(client.get("/api/messages").json()) + json.dumps(client.get("/api/memories").json())
        assert "Hello notes" not in blob


def test_cancel_expiry_forged_malformed_sensitive(tmp_path: Path):
    app = _app(tmp_path)
    clock = {"now": 0.0}
    app.state.browser_bridge._clock = lambda: clock["now"]
    app.state.browser_bridge.actions._clock = lambda: clock["now"]
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        session_id = _handshake(client, token, pair)
        _scan(client, token, session_id, [_BUTTON, _FIELD])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "focus", "element_id": _FIELD["element_id"]},
        ).json()
        cancelled = client.post("/api/browser/bridge/actions/cancel", json={}).json()
        assert cancelled["pending"] is None
        assert client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()["performed"] is False
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "select_all", "element_id": _FIELD["element_id"]},
        ).json()
        clock["now"] = 120.0
        expired = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        ).json()
        assert expired["text"] == "That approval has expired."
        clock["now"] = 200.0
        client.post("/api/browser/bridge/pair", json={})
        pair = client.get("/api/browser/bridge").json()["pair_code"]
        session_id = _handshake(client, token, pair)
        assert _scan(client, token, session_id, [_BUTTON])["ok"] is True
        forged = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": "0123456789abcdef0123456789abcdef", "approved": True},
        ).json()
        assert forged["performed"] is False
        bad = client.post(
            "/api/browser/bridge/ingress",
            headers={"X-Jarvis-Bridge-Token": token},
            json={
                "message": {
                    "type": "action_targets",
                    "session_id": session_id,
                    "browser": "Google Chrome",
                    "tab_title": "Example Docs",
                    "tab_url": "https://example.com",
                    "elements": [_BUTTON],
                    "extra": True,
                },
                "raw_size": 200,
            },
        ).json()
        assert bad == {"ok": False, "error": "invalid"}
        sensitive = _scan(
            client,
            token,
            session_id,
            [_BUTTON],
            title="Sign in",
            url="https://example.com/login",
        )
        assert sensitive == {"ok": False, "error": "targets"}
        passwordish = dict(_FIELD)
        passwordish["input_type"] = "password"
        passwordish["element_id"] = "eeeeeeeeeeeeeeee"
        passwordish["fingerprint"] = "ffffffffffffffffffffffffffffffff"
        assert _scan(client, token, session_id, [passwordish]) == {"ok": False, "error": "targets"}


def test_tab_element_change_and_verification_failure(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        session_id = _handshake(client, token, pair)
        _scan(client, token, session_id, [_BUTTON])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        )
        job = _claim(client, token, session_id)
        tab_changed = _result(client, token, session_id, job, title="Other Page", url="https://example.com/other")
        assert tab_changed == {"ok": False, "error": "tab"}
        _scan(client, token, session_id, [_BUTTON])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        )
        job = _claim(client, token, session_id)
        element_changed = _result(
            client,
            token,
            session_id,
            job,
            fingerprint="11111111111111111111111111111111",
        )
        assert element_changed == {"ok": False, "error": "element"}
        _scan(client, token, session_id, [_BUTTON])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": preview["pending"]["action_id"], "approved": True},
        )
        job = _claim(client, token, session_id)
        failed = _result(client, token, session_id, job, outcome="hidden", verified=False)
        assert failed == {"ok": False, "error": "verify"}
        assert client.get("/api/browser/bridge/actions").json()["result"]["outcome"] == "hidden"


def test_permission_disable_race_and_concurrent_confirm(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        session_id = _handshake(client, token, pair)
        _scan(client, token, session_id, [_BUTTON])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        client.post("/api/browser/bridge/actions/permission", json={"enabled": False})
        raced = client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": action_id, "approved": True},
        ).json()
        assert raced["ready"] is None
        assert raced["performed"] is False
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        _scan(client, token, session_id, [_BUTTON])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "click", "element_id": _BUTTON["element_id"]},
        ).json()
        action_id = preview["pending"]["action_id"]
        results: list[dict] = []

        def decide() -> None:
            results.append(
                client.post(
                    "/api/browser/bridge/actions/confirm",
                    json={"action_id": action_id, "approved": True},
                ).json()
            )

        threads = [threading.Thread(target=decide) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(1 for row in results if row.get("performed")) == 1
        assert client.get("/api/browser/bridge/actions").json()["ready"]["action_id"] == action_id


def test_emergency_stop_resume_no_replay_and_isolation(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        token, pair = _enable_bridge(client, app)
        client.post("/api/browser/bridge/actions/permission", json={"enabled": True})
        session_id = _handshake(client, token, pair)
        _scan(client, token, session_id, [_FIELD])
        preview = client.post(
            "/api/browser/bridge/actions/propose",
            json={"action": "type", "element_id": _FIELD["element_id"], "text": "Secret draft"},
        ).json()
        action_id = preview["pending"]["action_id"]
        assert client.post("/api/emergency-stop").json() == {"requested": True}
        stopped = client.get("/api/browser/bridge/actions").json()
        assert stopped["pending"] is None
        assert stopped["ready"] is None
        assert stopped["targets"] is None
        assert client.post("/api/resume").json() == {"resumed": True, "replayed": False}
        assert client.post(
            "/api/browser/bridge/actions/confirm",
            json={"action_id": action_id, "approved": True},
        ).json()["performed"] is False
        assert "Secret draft" not in json.dumps(client.get("/api/agent/goal").json())
        assert "Secret draft" not in json.dumps(client.get("/api/desktop/engine").json())
        assert "Secret draft" not in json.dumps(client.get("/api/agent/plan").json())
        assert client.get("/api/desktop/engine").json()["permission_enabled"] is False
