import threading
from pathlib import Path

import httpx

from jarvis.agent.loop import classify_after_call, run_turn
from jarvis.audit import AuditLog
from jarvis.config import load_settings
from jarvis.memory.store import ConversationStore
from jarvis.providers.base import ChatResult
from jarvis.providers.fake import FakeProvider
from jarvis.providers.gemini import GeminiProvider
from jarvis.providers.groq import GroqProvider
from jarvis.safety.halt import HaltController
from jarvis.skills.base import SkillSpec


def _stack(tmp_path: Path):
    halt = HaltController()
    store = ConversationStore(tmp_path / "jarvis.sqlite3")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return halt, store, audit


def test_fake_reply_is_stored_and_language_is_unchanged(tmp_path: Path):
    halt, store, audit = _stack(tmp_path)
    provider = FakeProvider()
    result = run_turn(
        user_text="kal meeting hai kya?",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert result.status == "completed"
    assert result.text == provider.reply
    assert provider.messages_seen[0][-1]["content"] == "kal meeting hai kya?"
    assert provider.messages_seen[0][0]["role"] == "system"
    assert store.visible()[-1]["status"] == "ok"


def test_provider_error_is_not_stored_as_success(tmp_path: Path):
    halt, store, audit = _stack(tmp_path)
    provider = FakeProvider()
    provider.mode = "error"
    result = run_turn(
        user_text="hello",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert result.status == "error"
    assert result.text.startswith("The provider returned an error")
    assert store.visible()[-1]["status"] == "failed"
    assert halt.status == "error"


def test_stop_during_turn_cancels_and_does_not_ask_the_model_to_stop(tmp_path: Path):
    halt, store, audit = _stack(tmp_path)
    provider = FakeProvider()
    provider.mode = "block"
    box = {}

    def go():
        box["result"] = run_turn(
            user_text="wait",
            provider=provider,
            halt=halt,
            store=store,
            audit=audit,
            timeout_s=5,
            max_steps=4,
        )

    thread = threading.Thread(target=go)
    thread.start()
    assert provider.entered.wait(2)
    calls_before_stop = provider.calls
    halt.emergency_stop()
    thread.join(2)
    assert calls_before_stop == 1
    assert provider.calls == 1
    joined = " ".join(message["content"] for batch in provider.messages_seen for message in batch)
    assert "Emergency Stop" not in joined
    assert box["result"].status == "stopped"
    assert box["result"].outcome == "cancelled"
    assert all(row["content"] != provider.reply for row in store.visible())


def test_reply_that_already_arrived_is_discarded(tmp_path: Path):
    halt, store, audit = _stack(tmp_path)

    class Late:
        id = "late"
        model = "late"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, messages, tool_specs, halt, generation, timeout_s):
            self.calls += 1
            halt.emergency_stop()
            return ChatResult(ok=True, text="already sent")

    provider = Late()
    result = run_turn(
        user_text="go",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert result.status == "stopped"
    assert result.outcome == "already_completed"
    assert result.text == ""
    assert all("already sent" not in row["content"] for row in store.visible())


def test_step_limit_and_tools_are_not_executed(tmp_path: Path):
    halt, store, audit = _stack(tmp_path)
    provider = FakeProvider()
    provider.mode = "tools"
    refused = run_turn(
        user_text="open a shell",
        provider=provider,
        halt=halt,
        store=store,
        audit=audit,
        timeout_s=5,
        max_steps=4,
    )
    assert refused.status == "error"
    assert refused.error_type == "tool_unavailable"
    assert provider.calls == 1
    assert "nothing was run" in refused.text

    halt2, store2, audit2 = _stack(tmp_path / "more")
    provider.calls = 0
    provider.release.set()
    limited = run_turn(
        user_text="again",
        provider=provider,
        halt=halt2,
        store=store2,
        audit=audit2,
        timeout_s=5,
        max_steps=4,
        follow_tools=True,
    )
    assert limited.status == "error"
    assert limited.error_type == "step_limit"
    assert provider.calls == 4


def test_classify_labels():
    arrived = ChatResult(ok=True, text="sent", outcome="already_completed")
    assert classify_after_call(arrived, False) == "already_completed"
    cancelled = ChatResult(ok=False, outcome="cancelled")
    assert classify_after_call(cancelled, False) == "cancelled"
    assert classify_after_call(ChatResult(ok=False), False) == "cancelled"


def test_startup_settings_do_not_resume_tasks(tmp_path: Path):
    (tmp_path / "resume.json").write_text('{"run": true}', encoding="utf-8")
    settings = load_settings(tmp_path, environ={})
    assert settings.provider == "fake"
    assert settings.host == "127.0.0.1"
    assert settings.max_steps == 4
    assert settings.timeout_s == 45
    assert settings.tool_timeout_s == 20
    assert SkillSpec("x", "y", "read").timeout_s == 20


def test_groq_and_gemini_use_the_outbound_gate(tmp_path: Path):
    halt = HaltController()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.host == "api.groq.com":
            assert request.headers["authorization"].startswith("Bearer ")
            return httpx.Response(200, json={"choices": [{"message": {"content": "groq says hi"}}]})
        assert request.headers["x-goog-api-key"] == "gem-key"
        assert "key=" not in str(request.url)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}]},
        )

    transport = httpx.MockTransport(handler)
    groq = GroqProvider("groq-key", "openai/gpt-oss-20b", transport=transport)
    result = groq.complete(
        messages=[{"role": "user", "content": "hello"}],
        tool_specs=[],
        halt=halt,
        generation=halt.capture(),
        timeout_s=5,
    )
    assert result.ok is True
    assert result.text == "groq says hi"
    gemini = GeminiProvider("gem-key", "gemini-2.5-flash", transport=transport)
    result = gemini.complete(
        messages=[{"role": "user", "content": "नमस्ते"}],
        tool_specs=[],
        halt=halt,
        generation=halt.capture(),
        timeout_s=5,
    )
    assert result.text == "gemini says hi"
    assert calls["n"] == 2

    halt.emergency_stop()
    blocked = groq.complete(
        messages=[{"role": "user", "content": "again"}],
        tool_specs=[],
        halt=halt,
        generation=halt.capture(),
        timeout_s=5,
    )
    assert blocked.outcome == "cancelled"
    assert calls["n"] == 2

    missing = GroqProvider("", "openai/gpt-oss-20b", transport=transport)
    halt.resume()
    missed = missing.complete(
        messages=[{"role": "user", "content": "hello"}],
        tool_specs=[],
        halt=halt,
        generation=halt.capture(),
        timeout_s=5,
    )
    assert missed.error_type == "missing_key"
    assert calls["n"] == 2


def test_store_keeps_history_and_can_search(tmp_path: Path):
    store = ConversationStore(tmp_path / "jarvis.sqlite3")
    store.add_message("user", "project meeting notes", "ok")
    assert store.search("meeting")
    store.close()
