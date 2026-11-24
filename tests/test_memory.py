from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.agent.loop import run_turn
from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.memory.store import ConversationStore
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController


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
    return create_app(settings=_settings(tmp_path), fake=fake or FakeProvider(), start_hotkeys_flag=False)


def test_create_list_search_edit_and_keep_chat_separate(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/memories", json={"fact": "I prefer chai in the morning"})
        assert created.status_code == 200
        assert created.json()["created"] is True
        memory_id = created.json()["memory"]["id"]

        client.post("/api/chat", json={"text": "Hello"})
        listed = client.get("/api/memories").json()["memories"]
        assert [row["fact"] for row in listed] == ["I prefer chai in the morning"]
        assert client.get("/api/messages").json()["messages"][-1]["content"] != listed[0]["fact"]

        found = client.get("/api/memories", params={"q": "chai"}).json()["memories"]
        assert len(found) == 1
        assert client.get("/api/memories", params={"q": "weather"}).json()["memories"] == []

        edited = client.post(f"/api/memories/{memory_id}", json={"fact": "I prefer coffee in the morning"})
        assert edited.status_code == 200
        assert edited.json()["memory"]["fact"] == "I prefer coffee in the morning"
        assert client.get("/api/memories", params={"q": "coffee"}).json()["memories"][0]["id"] == memory_id
        assert client.get("/api/memories", params={"q": "chai"}).json()["memories"] == []


def test_delete_and_clear_require_confirmation(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        first = client.post("/api/memories", json={"fact": "Project name is JARVIS"}).json()["memory"]["id"]
        second = client.post("/api/memories", json={"fact": "I work on a laptop"}).json()["memory"]["id"]

        refused = client.post(f"/api/memories/{first}/delete", json={"confirm": False})
        assert refused.status_code == 400
        assert client.get(f"/api/memories").json()["memories"]

        removed = client.post(f"/api/memories/{first}/delete", json={"confirm": True})
        assert removed.status_code == 200
        remaining = client.get("/api/memories").json()["memories"]
        assert [row["id"] for row in remaining] == [second]

        blocked = client.post("/api/memories/clear", json={"confirm": True, "phrase": "yes"})
        assert blocked.status_code == 400
        assert len(client.get("/api/memories").json()["memories"]) == 1

        cleared = client.post(
            "/api/memories/clear",
            json={"confirm": True, "phrase": "clear all memories"},
        )
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] == 1
        assert client.get("/api/memories").json()["memories"] == []


def test_invalid_memory_requests(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/memories", json={"fact": "   "}).status_code == 400
        secret = client.post("/api/memories", json={"fact": "my password is hunter2"})
        assert secret.status_code == 400
        assert "secret" in secret.json()["detail"].lower() or "not saved" in secret.json()["detail"].lower()
        assert client.get("/api/memories").json()["memories"] == []
        long_fact = "a" * 281
        assert client.post("/api/memories", json={"fact": long_fact}).status_code == 400
        assert client.post("/api/memories/99", json={"fact": "missing"}).status_code == 404
        assert client.post("/api/memories/1/delete", json={"confirm": True}).status_code == 404


def test_memory_persists_and_restart_does_not_replay(tmp_path: Path):
    settings = _settings(tmp_path)
    first = create_app(settings=settings, fake=FakeProvider(), start_hotkeys_flag=False)
    with TestClient(first) as client:
        client.post("/api/memories", json={"fact": "I prefer short answers"})
        client.post("/api/chat", json={"text": "finish this later"})
    first.state.store.close()

    second_provider = FakeProvider()
    second = create_app(settings=settings, fake=second_provider, start_hotkeys_flag=False)
    with TestClient(second) as client:
        client.get("/api/status")
        rows = client.get("/api/memories").json()["memories"]
    assert [row["fact"] for row in rows] == ["I prefer short answers"]
    assert second_provider.calls == 0
    second.state.store.close()


def test_explicit_remember_does_not_call_the_model_and_chat_does_not_save(tmp_path: Path):
    halt = HaltController()
    store = ConversationStore(tmp_path / "jarvis.sqlite3")
    audit = AuditLog(tmp_path / "audit.jsonl")
    provider = FakeProvider(reply="I will remember that you like mango")
    saved = run_turn(
        user_text="Remember that I prefer short answers",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert saved.status == "completed"
    assert "stored fact" in saved.text.lower()
    assert provider.calls == 0
    assert store.list_memories()[0]["fact"] == "I prefer short answers"

    spoken = run_turn(
        user_text="thanks",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert spoken.text == provider.reply
    assert len(store.list_memories()) == 1

    refused = run_turn(
        user_text="Delete memory 1",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert "did not delete" in refused.text.lower()
    assert len(store.list_memories()) == 1
    store.close()


def test_only_relevant_memories_are_sent(tmp_path: Path):
    halt = HaltController()
    store = ConversationStore(tmp_path / "jarvis.sqlite3")
    audit = AuditLog(tmp_path / "audit.jsonl")
    provider = FakeProvider(reply="local reply")
    store.add_memory("I prefer chai in the morning")
    store.add_memory("The laptop has limited RAM")
    run_turn(
        user_text="Should I buy more chai?",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    prompt = "\n".join(message["content"] for message in provider.messages_seen[-1])
    assert "I prefer chai in the morning" in prompt
    assert "limited RAM" not in prompt
    assert "not assumptions" in prompt

    run_turn(
        user_text="Hello",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    hello_prompt = "\n".join(message["content"] for message in provider.messages_seen[-1])
    assert "I prefer chai" not in hello_prompt
    store.close()


def test_search_and_show_all_do_not_drop_a_saved_memory(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/memories", json={"fact": "I prefer short answers."})
        assert created.status_code == 200
        memory_id = created.json()["memory"]["id"]

        found = client.get("/api/memories", params={"q": "short"})
        assert found.status_code == 200
        body = found.json()
        assert body["total"] == 1
        assert [row["fact"] for row in body["memories"]] == ["I prefer short answers."]

        missed = client.get("/api/memories", params={"q": "preference"})
        assert missed.json()["memories"] == []
        assert missed.json()["total"] == 1

        listed = client.get("/api/memories")
        assert listed.json()["total"] == 1
        assert [row["id"] for row in listed.json()["memories"]] == [memory_id]

        edited = client.post(f"/api/memories/{memory_id}", json={"fact": "I prefer brief answers."})
        assert edited.status_code == 200
        assert client.get("/api/memories", params={"q": "brief"}).json()["memories"][0]["fact"] == "I prefer brief answers."
        assert client.get("/api/memories").json()["total"] == 1

        untouched = client.post("/api/memories/clear", json={"confirm": True})
        assert untouched.status_code == 400
        assert client.get("/api/memories").json()["total"] == 1

        removed = client.post(f"/api/memories/{memory_id}/delete", json={"confirm": True})
        assert removed.status_code == 200
        assert client.get("/api/memories").json()["memories"] == []


def test_emergency_stop_blocks_memory_changes(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/memories", json={"fact": "Keep this fact"})
        memory_id = created.json()["memory"]["id"]
        client.post("/api/emergency-stop")
        assert client.post("/api/memories", json={"fact": "Another fact"}).status_code == 409
        assert client.post(f"/api/memories/{memory_id}", json={"fact": "Changed"}).status_code == 409
        assert client.post(f"/api/memories/{memory_id}/delete", json={"confirm": True}).status_code == 409
        assert client.post(
            "/api/memories/clear",
            json={"confirm": True, "phrase": "clear all memories"},
        ).status_code == 409
        assert client.post("/api/chat", json={"text": "Remember that I like rain"}).status_code == 409
        rows = client.get("/api/memories").json()["memories"]
        assert [row["fact"] for row in rows] == ["Keep this fact"]
