"""HTTP app, status page, and process entry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from jarvis.agent.loop import run_turn
from jarvis.audit import AuditLog
from jarvis.config import Settings, load_settings, project_root
from jarvis.memory.commands import validate_fact
from jarvis.memory.store import ConversationStore
from jarvis.providers import build_registry
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.hotkey import start_hotkeys
from jarvis.safety.policy import Policy
from jarvis.skills.registry import SkillRegistry
from jarvis.voice.diagnostic import (
    CONSENT_HEADER,
    DIAGNOSTIC_NOTICE,
    DIAGNOSTIC_TIMEOUT_S,
    DiagnosticStore,
)
from jarvis.voice.errors import TranscriptionError, VoiceCancelled
from jarvis.voice.gate import VoiceGate
from jarvis.voice.speak import EDGE_NOTICE, EdgeSpeaker
from jarvis.voice.transcribe import LOCAL_NOTICE, MAX_AUDIO_BYTES, build_transcriber, inspect_audio

STATIC_DIR = Path(__file__).resolve().parent / "static"
GEMINI_NOTICE = "Unpaid Gemini requests may be used to improve Google products."


class ChatIn(BaseModel):
    text: str = Field(max_length=8000)


class ProviderIn(BaseModel):
    provider: str
    model: str | None = None


class MemoryIn(BaseModel):
    fact: str = Field(max_length=400)


class ConfirmIn(BaseModel):
    confirm: bool = False
    phrase: str = ""


CLEAR_PHRASE = "clear all memories"


def end_process() -> None:
    os._exit(1)


def create_app(
    settings: Settings | None = None,
    halt: HaltController | None = None,
    fake: FakeProvider | None = None,
    start_hotkeys_flag: bool = False,
    end_process_fn=None,
    transcriber=None,
    speaker=None,
) -> FastAPI:
    settings = settings or load_settings(project_root())
    halt = halt or HaltController()
    fake = fake or FakeProvider()
    registry = build_registry(settings, fake=fake)
    skills = SkillRegistry()
    policy = Policy(settings.root)
    store = ConversationStore(settings.data_dir / "jarvis.sqlite3")
    audit = AuditLog(settings.data_dir / "audit.jsonl")
    voice_gate = VoiceGate()
    diagnostic = DiagnosticStore(settings.data_dir / "diagnostic")
    diagnostic.discard()
    transcriber = transcriber or build_transcriber(
        settings.data_dir / "whisper",
        settings.whisper_device,
        settings.whisper_model,
    )
    speaker = speaker or EdgeSpeaker(timeout_s=min(settings.voice_timeout_s, 25))
    stopper = end_process_fn or end_process
    if start_hotkeys_flag:
        start_hotkeys(halt, stopper)

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.halt = halt
    app.state.fake = fake
    app.state.registry = registry
    app.state.skills = skills
    app.state.policy = policy
    app.state.store = store
    app.state.audit = audit
    app.state.voice_gate = voice_gate
    app.state.diagnostic = diagnostic
    app.state.transcriber = transcriber
    app.state.speaker = speaker
    app.state.end_process = stopper

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status():
        body = halt.snapshot()
        provider = registry.active()
        body.update(
            {
                "audit": audit.tail(20),
                "provider": registry.active_id,
                "model": provider.model,
                "providers": registry.ids(),
                "permissions": {item.value: enabled for item, enabled in policy.enabled.items()},
                "skills": skills.ids(),
                "gemini_notice": GEMINI_NOTICE if registry.active_id == "gemini" else "",
                "voice": {
                    "transcription": "local-whisper-small",
                    "audio_leaves_machine": False,
                    "speech_notice": EDGE_NOTICE,
                    "microphone": "push-to-talk",
                },
            }
        )
        return body

    @app.get("/api/messages")
    def messages():
        return {"messages": store.visible()}

    def _require_running() -> None:
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")

    def _checked_fact(raw: str) -> str:
        fact, error = validate_fact(raw)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return fact or ""

    @app.get("/api/memories")
    def memories(q: str = ""):
        saved = store.list_memories()
        query = q.strip()
        rows = store.search_memories(query) if query else saved
        return {"memories": rows, "total": len(saved), "query": query}

    @app.post("/api/memories")
    def create_memory(body: MemoryIn):
        _require_running()
        fact = _checked_fact(body.fact)
        row, created = store.add_memory(fact)
        audit.write(status="completed", detail="Memory saved" if created else "Memory already saved")
        return {"memory": row, "created": created}

    @app.post("/api/memories/clear")
    def clear_memories(body: ConfirmIn):
        _require_running()
        phrase = " ".join(body.phrase.split()).lower()
        if body.confirm is not True or phrase != CLEAR_PHRASE:
            raise HTTPException(status_code=400, detail="Clearing all memories requires confirmation.")
        removed = store.clear_memories()
        audit.write(status="completed", detail="Memories cleared")
        return {"cleared": removed}

    @app.post("/api/memories/{memory_id}")
    def edit_memory(memory_id: int, body: MemoryIn):
        _require_running()
        fact = _checked_fact(body.fact)
        row = store.update_memory(memory_id, fact)
        if row is None:
            raise HTTPException(status_code=404, detail="No stored memory has that id.")
        audit.write(status="completed", detail="Memory updated")
        return {"memory": row}

    @app.post("/api/memories/{memory_id}/delete")
    def remove_memory(memory_id: int, body: ConfirmIn):
        _require_running()
        if body.confirm is not True:
            raise HTTPException(status_code=400, detail="Deletion requires confirmation.")
        if not store.delete_memory(memory_id):
            raise HTTPException(status_code=404, detail="No stored memory has that id.")
        audit.write(status="completed", detail="Memory deleted")
        return {"deleted": memory_id}

    @app.post("/api/emergency-stop")
    def emergency_stop():
        report = halt.emergency_stop()
        voice_gate.cancel()
        diagnostic.delete()
        for item in report["outcomes"]:
            audit.write(status="stopped", skill_id=item.get("action", ""), outcome=item["outcome"])
        audit.write(status="stopped", outcome="cancelled", detail="Emergency stop")
        return {"requested": True}

    @app.post("/api/resume")
    def resume():
        if not halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is not stopped.")
        halt.resume()
        audit.write(status="idle", detail="Manual resume. Previous tasks were not replayed.")
        return {"resumed": True, "replayed": False}

    @app.post("/api/quit")
    def quit_process():
        audit.write(status="error", detail="End process requested")
        stopper()
        return {"requested": True}

    @app.post("/api/provider")
    def set_provider(body: ProviderIn):
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        try:
            registry.select(body.provider, body.model)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail="Unknown provider.") from exc
        audit.write(status="idle", detail=f"Provider set to {registry.active_id}")
        notice = GEMINI_NOTICE if registry.active_id == "gemini" else ""
        return {"provider": registry.active_id, "model": registry.active().model, "notice": notice}

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Empty message.")
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        if not halt.try_begin():
            raise HTTPException(status_code=409, detail="The assistant is busy.")

        def work():
            return run_turn(
                user_text=text,
                provider=registry.active(),
                halt=halt,
                store=store,
                audit=audit,
                timeout_s=settings.timeout_s,
                max_steps=settings.max_steps,
            )

        try:
            result = await asyncio.to_thread(work)
        finally:
            halt.finish_busy()
        return {
            "status": result.status,
            "text": result.text,
            "outcome": result.outcome,
            "error_type": result.error_type,
        }

    @app.post("/api/voice/transcribe")
    async def transcribe_voice(request: Request):
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        if not halt.try_begin():
            raise HTTPException(status_code=409, detail="The assistant is busy.")
        generation = voice_gate.capture()
        started = False
        try:
            audio = await request.body()
            if not audio:
                raise HTTPException(status_code=400, detail="No audio was received.")
            if len(audio) > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=400, detail="That recording is too long.")
            content_type = request.headers.get("content-type", "")

            def work():
                try:
                    try:
                        measurement = inspect_audio(audio, content_type)
                        text = transcriber.transcribe(audio, content_type, halt, voice_gate, generation)
                    except VoiceCancelled:
                        return None
                    return text, measurement
                finally:
                    halt.finish_busy()

            def consume(finished: asyncio.Task) -> None:
                if not finished.cancelled():
                    finished.exception()

            started = True
            task = asyncio.create_task(asyncio.to_thread(work))
            task.add_done_callback(consume)
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=settings.voice_timeout_s)
            except TimeoutError:
                voice_gate.cancel()
                audit.write(status="error", detail="Voice transcription timed out")
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Transcription timed out. The clip is still in the page. "
                        "Wait until this attempt finishes, then press Transcribe clip again."
                    ),
                )
            except VoiceCancelled:
                audit.write(status="stopped", outcome="cancelled", detail="Voice transcription cancelled")
                raise HTTPException(status_code=409, detail="Voice was cancelled.")
            except TranscriptionError as exc:
                audit.write(status="error", detail=str(exc))
                raise HTTPException(status_code=502, detail=str(exc))
            if result is None or not voice_gate.current(generation, halt.halted):
                audit.write(status="stopped", outcome="cancelled", detail="Voice transcription discarded")
                raise HTTPException(status_code=409, detail="Voice was cancelled.")
            text, measurement = result
            audit.write(status="completed", detail="Voice transcribed locally")
            return {
                "text": text,
                "audio_leaves_machine": False,
                "notice": LOCAL_NOTICE,
                "audio": measurement,
            }
        finally:
            if not started:
                halt.finish_busy()

    @app.post("/api/voice/speak")
    async def speak_voice(body: ChatIn):
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Nothing to speak.")
        generation = voice_gate.capture()
        try:
            result = await speaker.speak(text, halt, voice_gate, generation)
        except VoiceCancelled:
            audit.write(status="stopped", outcome="cancelled", detail="Voice playback cancelled")
            raise HTTPException(status_code=409, detail="Voice was cancelled.")
        if result.engine == "none" or not result.text:
            raise HTTPException(status_code=400, detail="Nothing to speak.")
        if not voice_gate.current(generation, halt.halted):
            audit.write(status="stopped", outcome="cancelled", detail="Voice playback discarded")
            raise HTTPException(status_code=409, detail="Voice was cancelled.")
        audit.write(status="completed", detail=f"Voice reply prepared with {result.engine}")
        if result.audio:
            return Response(
                content=result.audio,
                media_type=result.media_type or "audio/mpeg",
                headers={
                    "X-Voice-Engine": result.engine,
                    "X-Voice-Notice": result.notice,
                    "Cache-Control": "no-store",
                },
            )
        return {"engine": result.engine, "text": result.text, "notice": result.notice, "replayed": False}

    @app.post("/api/voice/stop")
    def stop_voice():
        voice_gate.cancel()
        audit.write(status="idle", outcome="cancelled", detail="Voice stopped. Nothing was replayed.")
        return {"stopped": True, "replayed": False}

    @app.get("/api/voice/diagnostic")
    def diagnostic_status():
        return diagnostic.status()

    @app.post("/api/voice/diagnostic")
    async def keep_diagnostic(request: Request):
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        if request.headers.get("x-jarvis-diagnostic-consent", "") != CONSENT_HEADER:
            raise HTTPException(
                status_code=400,
                detail="Diagnostic audio is stored only after you agree to keep it on this computer.",
            )
        audio = await request.body()
        try:
            saved = diagnostic.save(audio, request.headers.get("content-type", ""))
        except ValueError as exc:
            if str(exc) == "long":
                raise HTTPException(status_code=400, detail="That recording is too long.") from exc
            raise HTTPException(status_code=400, detail="No audio was received.") from exc
        audit.write(status="completed", detail="Diagnostic clip kept locally")
        return saved

    @app.delete("/api/voice/diagnostic")
    def delete_diagnostic():
        removed = diagnostic.delete()
        if removed:
            audit.write(status="completed", detail="Diagnostic audio deleted")
        return {"deleted": removed, "audio_leaves_machine": False}

    @app.post("/api/voice/diagnostic/compare")
    async def compare_diagnostic():
        if halt.halted:
            diagnostic.delete()
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        if not halt.try_begin():
            raise HTTPException(status_code=409, detail="The assistant is busy.")
        generation = voice_gate.capture()
        try:
            try:
                audio, content_type = diagnostic.read()
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="No diagnostic recording is stored.")
            if not voice_gate.current(generation, halt.halted):
                raise HTTPException(status_code=409, detail="Voice was cancelled.")

            def work():
                return transcriber.compare(audio, content_type, halt, voice_gate, generation)

            try:
                runs = await asyncio.wait_for(asyncio.to_thread(work), timeout=DIAGNOSTIC_TIMEOUT_S)
            except TimeoutError:
                voice_gate.cancel()
                audit.write(status="error", detail="Diagnostic comparison timed out")
                raise HTTPException(
                    status_code=504,
                    detail="The diagnostic comparison timed out. The recording was deleted.",
                )
            except VoiceCancelled:
                audit.write(status="stopped", outcome="cancelled", detail="Diagnostic comparison cancelled")
                raise HTTPException(status_code=409, detail="Voice was cancelled.")
            except TranscriptionError as exc:
                audit.write(status="error", detail=str(exc))
                raise HTTPException(status_code=502, detail=str(exc))
            if not voice_gate.current(generation, halt.halted):
                audit.write(status="stopped", outcome="cancelled", detail="Diagnostic comparison discarded")
                raise HTTPException(status_code=409, detail="Voice was cancelled.")
            audit.write(status="completed", detail="Diagnostic comparison finished locally")
            return {
                "audio_leaves_machine": False,
                "deleted": True,
                "notice": DIAGNOSTIC_NOTICE,
                "runs": runs,
            }
        finally:
            diagnostic.delete()
            halt.finish_busy()

    return app


def main() -> None:
    import uvicorn

    settings = load_settings(project_root())
    application = create_app(settings=settings, start_hotkeys_flag=True)
    uvicorn.run(application, host=settings.host, port=settings.port, log_level="info")
