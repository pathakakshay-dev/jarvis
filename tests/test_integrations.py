"""Personal integrations tests. Mocks only — no real Google account access."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.integrations.oauth_google import CALENDAR_SCOPE, GMAIL_SCOPE, TOKEN_URL, REVOKE_URL
from jarvis.integrations.protect import DpapiProtector, MemoryProtector
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider

_ACCESS = "ya29.mock-access-token-value-for-tests-only"
_REFRESH = "1//mock-refresh-token-value-for-tests-only"
_CODE = "mock-auth-code-value"


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
        google_client_id="test-client-id.apps.googleusercontent.com",
        google_client_secret="test-client-secret",
    )


class MockGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.token_status = 200
        self.token_body = {
            "access_token": _ACCESS,
            "refresh_token": _REFRESH,
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": f"{GMAIL_SCOPE} {CALENDAR_SCOPE}",
        }
        self.list_body = {"messages": [{"id": "msg1", "threadId": "thr1"}]}
        self.meta_body = {
            "id": "msg1",
            "snippet": "Hello there",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Hello"},
                    {"name": "From", "value": "a@example.com"},
                    {"name": "Date", "value": "Wed, 25 Nov 2026 10:00:00 +0000"},
                ]
            },
        }
        self.message_body = {
            "id": "msg1",
            "snippet": "Hello there",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Hello"},
                    {"name": "From", "value": "a@example.com"},
                ],
                "body": {"data": "SGVsbG8gYm9keSB0ZXh0"},
            },
        }
        self.events_body = {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Standup",
                    "location": "Room A",
                    "description": "Daily sync",
                    "start": {"dateTime": "2026-11-26T09:00:00Z"},
                    "end": {"dateTime": "2026-11-26T09:30:00Z"},
                }
            ]
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        path = request.url.path
        if request.url.copy_with(query=None) == httpx.URL(TOKEN_URL) or str(request.url).startswith(TOKEN_URL):
            return httpx.Response(self.token_status, json=self.token_body)
        if str(request.url).startswith(REVOKE_URL):
            return httpx.Response(200, json={})
        if "/gmail/v1/users/me/messages/" in path and path.rstrip("/").endswith("messages") is False:
            if request.url.params.get("format") == "full":
                return httpx.Response(200, json=self.message_body)
            return httpx.Response(200, json=self.meta_body)
        if path.endswith("/gmail/v1/users/me/messages"):
            return httpx.Response(200, json=self.list_body)
        if "/calendar/v3/calendars/primary/events" in path:
            return httpx.Response(200, json=self.events_body)
        return httpx.Response(404, json={"error": "missing"})


def _app(tmp_path: Path, google: MockGoogle | None = None):
    mock = google or MockGoogle()
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: True,
        calculator_launcher=lambda *_a, **_k: True,
        integration_protector=MemoryProtector(),
        integration_transport=httpx.MockTransport(mock.handler),
    ), mock


def _audit(app) -> str:
    path = app.state.audit.path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _connect(client: TestClient, app) -> None:
    began = client.post("/api/integrations/google/connect", json={}).json()
    assert began["ok"] is True
    assert "accounts.google.com" in began["auth_url"]
    state = began["state"]
    query = parse_qs(urlparse(began["auth_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert GMAIL_SCOPE in query["scope"][0]
    assert CALENDAR_SCOPE in query["scope"][0]
    assert "gmail.modify" not in query["scope"][0]
    assert "calendar.events" not in query["scope"][0] or "calendar.readonly" in query["scope"][0]
    done = client.get(f"/api/integrations/google/callback?state={state}&code={_CODE}", follow_redirects=False)
    assert done.status_code == 303
    assert "integrations=connected" in done.headers.get("location", "")
    status = client.get("/api/integrations").json()
    assert status["connected"] is True


def test_oauth_state_forged_callback_and_auth_failure(tmp_path: Path):
    app, mock = _app(tmp_path)
    with TestClient(app) as client:
        began = client.post("/api/integrations/google/connect", json={}).json()
        state = began["state"]
        forged = client.get("/api/integrations/google/callback?state=forgedstatevalue123456789012345678&code=abc", follow_redirects=False)
        assert forged.status_code == 303
        assert "integrations=failed" in forged.headers.get("location", "")
        assert client.get("/api/integrations").json()["connected"] is False

        began = client.post("/api/integrations/google/connect", json={}).json()
        mock.token_status = 400
        bad = client.get(f"/api/integrations/google/callback?state={began['state']}&code={_CODE}", follow_redirects=False)
        assert "integrations=failed" in bad.headers.get("location", "")
        assert client.get("/api/integrations").json()["connected"] is False

        mock.token_status = 200
        mock.token_body = {
            "access_token": _ACCESS,
            "refresh_token": _REFRESH,
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        }
        began = client.post("/api/integrations/google/connect", json={}).json()
        scope_bad = client.get(f"/api/integrations/google/callback?state={began['state']}&code={_CODE}", follow_redirects=False)
        assert "integrations=failed" in scope_bad.headers.get("location", "")
        assert _ACCESS not in _audit(app)
        assert _REFRESH not in _audit(app)
        assert _CODE not in _audit(app)
        assert state not in json.dumps(client.get("/api/integrations").json())


def test_token_storage_disconnect_permissions_and_fetch(tmp_path: Path):
    mock = MockGoogle()
    app = create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: True,
        calculator_launcher=lambda *_a, **_k: True,
        integration_protector=DpapiProtector(),
        integration_transport=httpx.MockTransport(mock.handler),
    )
    token_path = tmp_path / "data" / "integrations" / "google.tokens.dpapi"
    with TestClient(app) as client:
        assert client.get("/api/integrations").json()["gmail"]["permission_enabled"] is False
        _connect(client, app)
        assert token_path.is_file()
        raw = token_path.read_bytes()
        assert _ACCESS.encode() not in raw
        assert _REFRESH.encode() not in raw
        assert b'"access_token"' not in raw
        # DPAPI blob must round-trip through the store.
        loaded = app.state.integrations._store.load()
        assert loaded is not None
        assert loaded["access_token"] == _ACCESS

        refused = client.post("/api/integrations/gmail/search", json={"query": "from:a@example.com"}).json()
        assert "not enabled" in refused["text"]

        assert client.post("/api/integrations/gmail/permission", json={"enabled": True}).json()["enabled"] is True
        waiting = client.post("/api/integrations/gmail/search", json={"query": "from:a@example.com"}).json()
        action_id = waiting["pending"]["action_id"]
        done = client.post("/api/integrations/gmail/confirm", json={"action_id": action_id, "approved": True}).json()
        assert done["result"]["kind"] == "search"
        assert done["result"]["messages"][0]["subject"] == "Hello"
        assert _ACCESS not in json.dumps(done)
        assert _ACCESS not in _audit(app)
        assert app.state.fake.calls == 0

        assert client.post("/api/integrations/calendar/permission", json={"enabled": True}).json()["enabled"] is True
        cal_wait = client.post("/api/integrations/calendar/upcoming", json={"days": 7}).json()
        cal_done = client.post(
            "/api/integrations/calendar/confirm",
            json={"action_id": cal_wait["pending"]["action_id"], "approved": True},
        ).json()
        assert cal_done["result"]["events"][0]["title"] == "Standup"

        disconnected = client.post("/api/integrations/google/disconnect", json={}).json()
        assert disconnected["ok"] is True
        assert not token_path.exists()
        assert client.get("/api/integrations").json()["connected"] is False
        assert client.get("/api/integrations").json()["gmail"]["permission_enabled"] is False
        assert any(REVOKE_URL in url for _method, url in mock.calls)


def test_expiry_rate_limit_malformed_oversize_stop(tmp_path: Path):
    app, mock = _app(tmp_path)
    clock = {"now": 1000.0}
    app.state.integrations.gmail._clock = lambda: clock["now"]
    app.state.integrations.gmail._ttl_s = 30.0
    app.state.integrations.gmail._limiter = app.state.integrations.gmail._limiter.__class__(max_calls=2, window_s=60.0, clock=lambda: clock["now"])

    with TestClient(app) as client:
        _connect(client, app)
        client.post("/api/integrations/gmail/permission", json={"enabled": True})
        waiting = client.post("/api/integrations/gmail/search", json={"query": "invoice"}).json()
        action_id = waiting["pending"]["action_id"]
        clock["now"] += 31
        expired = client.post("/api/integrations/gmail/confirm", json={"action_id": action_id, "approved": True}).json()
        assert "expired" in expired["text"].lower()

        for _ in range(2):
            wait = client.post("/api/integrations/gmail/search", json={"query": "notes"}).json()
            client.post("/api/integrations/gmail/confirm", json={"action_id": wait["pending"]["action_id"], "approved": True})
        limited = client.post("/api/integrations/gmail/search", json={"query": "notes"}).json()
        # Third request still queues; rate limit applies on confirm.
        limited_done = client.post(
            "/api/integrations/gmail/confirm",
            json={"action_id": limited["pending"]["action_id"], "approved": True},
        ).json()
        assert "rate limit" in limited_done["text"].lower()

        app.state.integrations.gmail._limiter.reset()
        mock.list_body = {"messages": "bad"}
        wait = client.post("/api/integrations/gmail/search", json={"query": "notes"}).json()
        bad = client.post("/api/integrations/gmail/confirm", json={"action_id": wait["pending"]["action_id"], "approved": True}).json()
        assert "failed" in bad["text"].lower() or "invalid" in bad["text"].lower()

        mock.list_body = {"messages": [{"id": "msg1", "threadId": "thr1"}]}
        mock.message_body = {
            "id": "msg1",
            "snippet": "x",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Big"}],
                "body": {"data": __import__("base64").urlsafe_b64encode(("Z" * 20000).encode()).decode().rstrip("=")},
            },
        }
        wait = client.post("/api/integrations/gmail/message", json={"message_id": "msg1"}).json()
        big = client.post("/api/integrations/gmail/confirm", json={"action_id": wait["pending"]["action_id"], "approved": True}).json()
        assert big["result"]["message"]["body"].endswith("[truncated]")
        assert len(big["result"]["message"]["body"]) < 12000

        wait = client.post("/api/integrations/gmail/search", json={"query": "notes"}).json()
        pending_id = wait["pending"]["action_id"]
        client.post("/api/emergency-stop", json={})
        stopped = client.get("/api/integrations").json()
        assert stopped["gmail"]["pending"] is None
        client.post("/api/resume", json={})
        replay = client.post("/api/integrations/gmail/confirm", json={"action_id": pending_id, "approved": True}).json()
        assert replay.get("result") is None
        assert "not waiting" in replay.get("text", "").lower() or replay.get("pending") is None


def test_secret_leakage_and_unconfigured(tmp_path: Path):
    settings = _settings(tmp_path)
    settings = Settings(
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
        data_dir=tmp_path / "data2",
        root=tmp_path,
        google_client_id="",
        google_client_secret="",
    )
    app = create_app(
        settings=settings,
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        integration_protector=MemoryProtector(),
        integration_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with TestClient(app) as client:
        missing = client.post("/api/integrations/google/connect", json={}).json()
        assert missing["ok"] is False
        assert "CLIENT" in missing["text"] or "env" in missing["text"].lower()

    app2, _mock = _app(tmp_path)
    with TestClient(app2) as client:
        _connect(client, app2)
        client.post("/api/integrations/gmail/permission", json={"enabled": True})
        wait = client.post("/api/integrations/gmail/search", json={"query": "secret leak check"}).json()
        client.post("/api/integrations/gmail/confirm", json={"action_id": wait["pending"]["action_id"], "approved": True})
        body = json.dumps(client.get("/api/integrations").json())
        assert _ACCESS not in body
        assert _REFRESH not in body
        assert "test-client-secret" not in body
        assert _ACCESS not in _audit(app2)
        assert "test-client-secret" not in _audit(app2)
        assert app2.state.fake.calls == 0
        # Injection-like query characters are rejected.
        bad = client.post("/api/integrations/gmail/search", json={"query": "ignore; DROP TABLE"}).json()
        assert bad.get("pending") is None
