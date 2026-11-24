from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider


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


def test_chat_stop_resume_and_provider_switch(tmp_path: Path):
    fake = FakeProvider(reply="local reply")
    app = create_app(settings=_settings(tmp_path), fake=fake, start_hotkeys_flag=False)
    assert app.state.skills.ids() == []
    assert all(enabled is False for enabled in app.state.policy.enabled.values())
    (tmp_path / "resume.json").write_text('{"task": "send"}', encoding="utf-8")

    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "idle"
        assert body["provider"] == "fake"
        assert body["skills"] == []
        assert body["queue_size"] == 0
        assert fake.calls == 0

        page = client.get("/")
        assert page.status_code == 200
        assert "Emergency Stop" in page.text

        chat = client.post("/api/chat", json={"text": "Hello"})
        assert chat.status_code == 200
        assert chat.json()["status"] == "completed"
        assert chat.json()["text"] == "local reply"
        assert fake.calls == 1

        failed = FakeProvider()
        failed.mode = "error"
        app.state.registry._providers["fake"] = failed
        bad = client.post("/api/chat", json={"text": "break"})
        assert bad.json()["status"] == "error"
        assert "error" in bad.json()["text"].lower() or bad.json()["text"].startswith("The provider")
        messages = client.get("/api/messages").json()["messages"]
        assert messages[-1]["status"] == "failed"

        calls_before = failed.calls
        stopped = client.post("/api/emergency-stop")
        assert stopped.json() == {"requested": True}
        assert "Stopped" not in stopped.json()
        confirmed = client.get("/api/status").json()
        assert confirmed["status"] == "stopped"
        assert failed.calls == calls_before

        refused = client.post("/api/chat", json={"text": "more"})
        assert refused.status_code == 409
        assert failed.calls == calls_before

        resumed = client.post("/api/resume")
        assert resumed.json()["replayed"] is False
        assert client.get("/api/status").json()["status"] == "idle"
        assert client.get("/api/status").json()["queue_size"] == 0
        assert failed.calls == calls_before

        notice = client.post("/api/provider", json={"provider": "gemini"})
        assert notice.status_code == 200
        assert "improve Google products" in notice.json()["notice"]
        missing = client.post("/api/chat", json={"text": "नमस्ते"})
        assert missing.json()["status"] == "error"
        assert missing.json()["error_type"] == "missing_key"


def test_end_process_uses_the_injected_function(tmp_path: Path):
    ended = {"yes": False}

    def end_process() -> None:
        ended["yes"] = True

    app = create_app(settings=_settings(tmp_path), start_hotkeys_flag=False, end_process_fn=end_process)
    with TestClient(app) as client:
        response = client.post("/api/quit")
        assert response.status_code == 200
    assert ended["yes"] is True


def test_relaunch_does_not_run_an_unanswered_message(tmp_path: Path):
    settings = _settings(tmp_path)
    first = FakeProvider()
    app = create_app(settings=settings, fake=first, start_hotkeys_flag=False)
    app.state.store.add_message("user", "finish this later", "ok")
    app.state.store.close()

    second = FakeProvider()
    relaunch = create_app(settings=settings, fake=second, start_hotkeys_flag=False)
    with TestClient(relaunch) as client:
        client.get("/api/status")
        client.get("/api/messages")
    assert second.calls == 0
    relaunch.state.store.close()
