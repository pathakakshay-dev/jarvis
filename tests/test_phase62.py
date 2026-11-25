"""Local API authentication, pinned research connections, and planner secret checks."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as RAW_TEST_CLIENT

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.research.web import WebResearch, public_https_url
from jarvis.safety.halt import HaltController
from jarvis.safety.net import PinnedBackend, PinnedTransport, resolve_public_https
from jarvis.safety.secrets import looks_like_secret, redact_known_secrets
from jarvis.skills.planner import PLANNER_SYSTEM, propose_plan


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
    )


def _raw(app):
    return RAW_TEST_CLIENT(app, base_url="http://127.0.0.1:8765")


def _records(*ips: str):
    def resolver(host, port, family, socktype):
        rows = []
        for ip in ips:
            rows.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
        return rows

    return resolver


def test_api_requires_the_process_token_and_hides_it(tmp_path: Path):
    app = _app(tmp_path)
    token = app.state.api_token
    with _raw(app) as client:
        missing = client.get("/api/status")
        wrong = client.get("/api/status", headers={"X-Jarvis-Token": "not-the-token"})
        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert missing.text == wrong.text
        assert token not in missing.text
        assert "api_token" not in missing.text
        for path in (
            "/api/skills/write",
            "/api/skills/write/confirm",
            "/api/skills/write/permission",
            "/api/agent/plan",
            "/api/agent/plan/confirm",
            "/api/agent/plan/cancel",
            "/api/agent/goal",
            "/api/agent/goal/start",
            "/api/agent/goal/confirm",
            "/api/agent/goal/cancel",
            "/api/memories",
            "/api/emergency-stop",
            "/api/resume",
            "/api/quit",
            "/api/desktop/notepad",
            "/api/desktop/notepad/confirm",
            "/api/desktop/notepad/permission",
            "/api/desktop/calculator",
            "/api/desktop/calculator/confirm",
            "/api/desktop/calculator/permission",
            "/api/desktop/control/preview",
            "/api/desktop/control/confirm",
            "/api/desktop/control/cancel",
            "/api/desktop/control/permission",
            "/api/desktop/focus/preview",
            "/api/desktop/focus/confirm",
            "/api/desktop/focus/cancel",
            "/api/desktop/focus/permission",
        ):
            refused = client.post(path, json={})
            assert refused.status_code == 401
            assert token not in refused.text
        foreign = client.get("/", headers={"Host": "evil.example:8765"})
        assert foreign.status_code == 401
        assert token not in foreign.text
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert token in page.text
        assert "__JARVIS_TOKEN__" not in page.text
        status = client.get("/api/status")
        assert status.status_code == 200
        assert token not in status.text


def test_confirm_and_cross_site_requests_cannot_approve(tmp_path: Path):
    app = _app(tmp_path)
    token = app.state.api_token
    workspace = tmp_path / "workspace"
    with TestClient(app) as client:
        client.post("/api/skills/write/permission", json={"enabled": True})
        pending = client.post("/api/skills/write", json={"path": "owned.txt", "content": "pwned"}).json()
        action_id = pending["action_id"]
    with _raw(app) as client:
        forged = client.post(
            "/api/skills/write/confirm",
            json={"action_id": action_id, "approved": True},
        )
        assert forged.status_code == 401
        crossed = client.post(
            "/api/skills/write/confirm",
            json={"action_id": action_id, "approved": True},
            headers={
                "X-Jarvis-Token": token,
                "Origin": "http://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert crossed.status_code == 401
        assert token not in crossed.text
        form = client.post(
            "/api/skills/write/confirm",
            data={"action_id": action_id, "approved": "true"},
            headers={"Origin": "http://evil.example"},
        )
        assert form.status_code == 401
    assert not (workspace / "owned.txt").exists()
    with TestClient(app) as client:
        allowed = client.post(
            "/api/skills/write/confirm",
            json={"action_id": action_id, "approved": False},
            headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["outcome"] == "denied"
        assert not (workspace / "owned.txt").exists()


def test_public_address_checks_cover_ipv4_ipv6_and_mixed_answers():
    assert public_https_url("https://example.com/a", _records("1.1.1.1")) is True
    assert public_https_url("https://example.com/a", _records("8.8.8.8")) is True
    assert public_https_url("https://example.com/a", _records("2001:4860:4860::8888")) is True
    for blocked in (
        "127.0.0.1",
        "10.1.1.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd00::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",
    ):
        assert public_https_url("https://example.com/a", _records(blocked)) is False, blocked
    assert public_https_url("https://example.com/a", _records("1.1.1.1", "10.0.0.1")) is False
    assert public_https_url("https://127.0.0.1/secret", _records("1.1.1.1")) is False
    assert public_https_url("https://[::1]/secret", _records("1.1.1.1")) is False
    assert public_https_url("https://metadata.google.internal/computeMetadata/v1/", _records("1.1.1.1")) is False
    assert public_https_url("http://example.com/a", _records("1.1.1.1")) is False
    assert public_https_url("https://user:pass@example.com/a", _records("1.1.1.1")) is False


def test_pinned_connection_does_not_resolve_again_or_dial_a_private_address():
    calls = {"n": 0}

    def resolver(host, port, family, socktype):
        calls["n"] += 1
        if calls["n"] == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    target = resolve_public_https("https://example.com/weather", resolver)
    assert target is not None
    assert target.ip == "1.1.1.1"
    assert calls["n"] == 1
    seen = {}

    class Record(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["host"] = request.headers["host"]
            seen["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, text="ok")

        def close(self) -> None:
            return None

    transport = PinnedTransport(target.ip, target.hostname, inner=Record())
    with httpx.Client(transport=transport, follow_redirects=False) as client:
        response = client.get("https://example.com/weather")
    assert response.status_code == 200
    assert calls["n"] == 1
    assert "1.1.1.1" in seen["url"]
    assert "127.0.0.1" not in seen["url"]
    assert seen["host"] == "example.com"
    assert seen["sni"] == "example.com"

    class Inner:
        def __init__(self) -> None:
            self.hosts: list[str] = []

        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            self.hosts.append(host)
            raise httpx.ConnectError("stopped")

    inner = Inner()
    backend = PinnedBackend("1.1.1.1", inner=inner)  # type: ignore[arg-type]
    try:
        backend.connect_tcp("example.com", 443)
    except Exception as exc:
        assert type(exc).__name__ == "ConnectError"
    assert inner.hosts == []
    try:
        PinnedBackend("10.0.0.1").connect_tcp("10.0.0.1", 443)
    except Exception as exc:
        assert type(exc).__name__ == "ConnectError"
    try:
        backend.connect_unix_socket("/tmp/jarvis.sock")
    except Exception as exc:
        assert type(exc).__name__ == "ConnectError"


def test_research_does_not_follow_a_redirect_to_a_private_address(tmp_path: Path):
    seen: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    researcher = WebResearch(
        search_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"results": [{"title": "Desk", "url": "https://example.com/weather", "content": "Clear."}]},
            )
        ),
        page_transport=httpx.MockTransport(page),
        resolver=_records("1.1.1.1"),
    )
    packet = researcher.gather("weather", HaltController(), HaltController().capture())
    assert seen == ["https://example.com/weather"]
    assert packet.sources[0].retrieved is False
    assert "127.0.0.1" not in packet.evidence
    assert "secret" not in packet.evidence


def test_private_search_resolution_does_not_send_the_request():
    researcher = WebResearch(resolver=_records("10.1.1.1"), provider="tavily")
    packet = researcher.gather("weather", HaltController(), HaltController().capture())
    assert packet.ok is False
    brave = WebResearch("not-a-real-key", resolver=_records("127.0.0.1"), provider="brave")
    refused = brave.gather("weather", HaltController(), HaltController().capture())
    assert refused.ok is False
    assert "not-a-real-key" not in refused.text


def test_planner_rejects_credential_shapes_and_sends_only_the_task():
    provider = FakeProvider()
    halt = HaltController()
    samples = [
        "read note.txt password=hunter2",
        "read note.txt token=abc123token",
        "note AKIAIOSFODNN7EXAMPLE",
        "note " + "ghp_" + ("a" * 36),
        "note " + "github_pat_" + ("a" * 30),
        "note AIza" + ("A" * 30),
        "note xoxb-" + ("1" * 12),
        "note npm_" + ("b" * 36),
        "note sk_live_" + ("c" * 16),
        "note eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0In0.signature1",
        "note -----BEGIN OPENSSH PRIVATE KEY-----",
    ]
    for task in samples:
        try:
            propose_plan(task, provider, halt, 5)
        except Exception as exc:
            assert "hunter2" not in str(exc)
            assert "AKIA" not in str(exc)
            assert "ghp_" not in str(exc)
        else:
            raise AssertionError(task)
    assert provider.calls == 0
    assert provider.messages_seen == []
    harmless = "read note.txt"
    assert looks_like_secret(harmless) is False
    assert looks_like_secret("write notes/password.txt with hello") is False
    assert looks_like_secret("the token bucket waits") is False
    propose_plan(harmless, provider, halt, 5)
    assert provider.calls == 1
    messages = provider.messages_seen[0]
    assert [item["role"] for item in messages] == ["system", "user"]
    assert messages[0]["content"] == PLANNER_SYSTEM
    assert messages[1]["content"] == harmless
    assert provider.tool_specs_seen[0] == []
    blob = "\n".join(item["content"] for item in messages)
    assert "JARVIS_GROQ_API_KEY" not in blob
    assert "hunter2" not in blob
    redacted = redact_known_secrets("note AKIAIOSFODNN7EXAMPLE and ghp_" + ("a" * 36))
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "ghp_" not in redacted


def test_audit_redacts_added_credential_shapes(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write(status="error", skill_id="ghp_" + ("a" * 36), detail="AKIAIOSFODNN7EXAMPLE")
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "ghp_" not in text
    assert "AKIAIOSFODNN7EXAMPLE" not in text
