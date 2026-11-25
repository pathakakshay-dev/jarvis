"""HTTP app, status page, and process entry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from jarvis.agent.loop import run_turn
from jarvis.audit import AuditLog
from jarvis.desktop.plan import DesktopPlanError, propose_desktop_plan
from jarvis.desktop.safety import DesktopSafety
from jarvis.desktop.simulator import DesktopSimulator
from jarvis.config import Settings, load_settings, project_root
from jarvis.memory.commands import validate_fact
from jarvis.memory.store import ConversationStore
from jarvis.providers import build_registry
from jarvis.providers.fake import FakeProvider
from jarvis.research.web import WebResearch
from jarvis.safety.halt import HaltController
from jarvis.safety.local_auth import api_allowed, local_page_allowed, new_api_token
from jarvis.safety.hotkey import start_hotkeys
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.base import SkillResult
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.files import (
    CreateFolder,
    ListFiles,
    ReadFile,
    SearchFiles,
    WriteFile,
    _FileError,
    classify_write,
    prepare_folder,
)
from jarvis.skills.apps import AppInventory
from jarvis.skills.clipboard import ClipboardRead
from jarvis.skills.orchestrator import SkillOrchestrator
from jarvis.skills.system import SystemInfo
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
    research: bool = False


class ProviderIn(BaseModel):
    provider: str
    model: str | None = None


class MemoryIn(BaseModel):
    fact: str = Field(max_length=400)


class ConfirmIn(BaseModel):
    confirm: bool = False
    phrase: str = ""


class SkillPathIn(BaseModel):
    path: str = Field(max_length=240)


class SkillQueryIn(BaseModel):
    query: str = Field(max_length=80)


class SkillWriteIn(BaseModel):
    path: str = Field(max_length=240)
    content: str = Field(max_length=100_000)


class SkillDecisionIn(BaseModel):
    action_id: str = Field(min_length=1, max_length=80)
    approved: bool


class AgentTaskIn(BaseModel):
    task: str = Field(max_length=800)


class DesktopTaskIn(BaseModel):
    task: str = Field(max_length=400)


class DesktopSimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: str = Field(max_length=40)
    application: str = Field(max_length=40)
    description: str = Field(max_length=160)
    summary: str = Field(max_length=160)
    risk: str = Field(max_length=10)


class SystemRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadPermissionIn(BaseModel):
    enabled: bool


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
    researcher = WebResearch(settings.brave_api_key, provider=settings.search_provider)
    skills.register(researcher)
    workspace = settings.root / "workspace"
    skills.register(ReadFile(workspace, halt))
    skills.register(ListFiles(workspace, halt))
    skills.register(SearchFiles(workspace, halt))
    skills.register(WriteFile(workspace, halt))
    skills.register(CreateFolder(workspace, halt))
    skills.register(SystemInfo(halt))
    skills.register(AppInventory(halt))
    skills.register(ClipboardRead(halt))
    policy = Policy(settings.root)
    store = ConversationStore(settings.data_dir / "jarvis.sqlite3")
    audit = AuditLog(settings.data_dir / "audit.jsonl")
    desktop_safety = DesktopSafety(halt, audit)
    desktop_simulator = DesktopSimulator(desktop_safety)
    skill_executor = SkillExecutor(skills, policy, halt, audit)
    orchestrator = SkillOrchestrator(
        skill_executor,
        skills,
        halt,
        audit,
        workspace,
        policy,
        registry.active,
        settings.timeout_s,
    )
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

    app = FastAPI(title="JARVIS", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.api_token = new_api_token()
    app.state.settings = settings
    app.state.halt = halt
    app.state.fake = fake
    app.state.registry = registry
    app.state.skills = skills
    app.state.researcher = researcher
    app.state.policy = policy
    app.state.store = store
    app.state.audit = audit
    app.state.desktop_safety = desktop_safety
    app.state.desktop_simulator = desktop_simulator
    app.state.skill_executor = skill_executor
    app.state.orchestrator = orchestrator
    app.state.voice_gate = voice_gate
    app.state.diagnostic = diagnostic
    app.state.transcriber = transcriber
    app.state.speaker = speaker
    app.state.end_process = stopper

    @app.middleware("http")
    async def require_local_client(request: Request, call_next):
        port = settings.port
        if not local_page_allowed(request, port):
            return JSONResponse({"detail": "Unauthorized."}, status_code=401)
        if request.url.path.startswith("/api/") and not api_allowed(request, app.state.api_token, port):
            return JSONResponse({"detail": "Unauthorized."}, status_code=401)
        return await call_next(request)

    @app.get("/")
    def index():
        raw = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        token = app.state.api_token
        if raw.count("__JARVIS_TOKEN__") != 1:
            return Response(status_code=500, content="The page is unavailable.")
        return HTMLResponse(raw.replace("__JARVIS_TOKEN__", token, 1))

    @app.get("/api/status")
    def status():
        desktop_safety.observe_stop()
        desktop_simulator.observe_stop()
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

    def _skill_payload(result, path: str, skill: str) -> dict:
        return {
            "ok": result.ok,
            "text": result.text,
            "outcome": result.outcome,
            "error_type": result.error_type,
            "action_id": result.action_id,
            "skill": skill,
            "path": path,
        }

    def _plan_action(action_id: str) -> bool:
        return bool(action_id) and action_id == orchestrator.current_action_id()

    def _visible_pending(skill: str) -> list[dict]:
        hidden = orchestrator.current_action_id()
        return [row for row in skill_executor.pending() if row["skill"] == skill and row["action_id"] != hidden]

    def _confirm_skill(action_id: str, approved: bool, skill: str) -> dict:
        path = skill_executor.requested_path(action_id, skill)
        if _plan_action(action_id) or skill_executor.pending_skill(action_id) != skill:
            result = SkillResult(
                False,
                text="That action is not waiting for approval.",
                outcome="rejected",
                error_type="unknown_action",
            )
            return _skill_payload(result, path, skill)
        return _skill_payload(skill_executor.confirm(action_id, approved=approved), path, skill)

    @app.get("/api/skills/read")
    def read_skill_state():
        pending = _visible_pending("read_file")
        return {"permission_enabled": bool(policy.enabled[Permission.READ]), "pending": pending}

    @app.post("/api/skills/read")
    def request_read(body: SkillPathIn):
        path = body.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="Say which file to read.")
        result = skill_executor.submit("read_file", {"path": path})
        return _skill_payload(result, path, "read_file")

    @app.post("/api/skills/read/confirm")
    def confirm_read(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "read_file")

    @app.post("/api/skills/read/permission")
    def set_read_permission(body: ReadPermissionIn):
        policy.enabled[Permission.READ] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="read_file",
            detail="Read File permission enabled" if body.enabled else "Read File permission disabled",
        )
        return {"permission": "read", "enabled": bool(policy.enabled[Permission.READ])}

    @app.get("/api/skills/list")
    def list_skill_state():
        pending = _visible_pending("list_files")
        return {"permission_enabled": bool(policy.enabled[Permission.LIST]), "pending": pending}

    @app.post("/api/skills/list")
    def request_list(body: SkillPathIn):
        path = body.path.strip()
        result = skill_executor.submit("list_files", {"path": path})
        return _skill_payload(result, path, "list_files")

    @app.post("/api/skills/list/confirm")
    def confirm_list(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "list_files")

    @app.post("/api/skills/list/permission")
    def set_list_permission(body: ReadPermissionIn):
        policy.enabled[Permission.LIST] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="list_files",
            detail="List Files permission enabled" if body.enabled else "List Files permission disabled",
        )
        return {"permission": "list", "enabled": bool(policy.enabled[Permission.LIST])}

    @app.get("/api/skills/search")
    def search_skill_state():
        pending = _visible_pending("search_files")
        return {"permission_enabled": bool(policy.enabled[Permission.SEARCH]), "pending": pending}

    @app.post("/api/skills/search")
    def request_search(body: SkillQueryIn):
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Say which filename to search for.")
        result = skill_executor.submit("search_files", {"query": query})
        return _skill_payload(result, query, "search_files")

    @app.post("/api/skills/search/confirm")
    def confirm_search(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "search_files")

    @app.post("/api/skills/search/permission")
    def set_search_permission(body: ReadPermissionIn):
        policy.enabled[Permission.SEARCH] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="search_files",
            detail="Search Files permission enabled" if body.enabled else "Search Files permission disabled",
        )
        return {"permission": "search", "enabled": bool(policy.enabled[Permission.SEARCH])}

    @app.get("/api/skills/write")
    def write_skill_state():
        pending = _visible_pending("write_file")
        return {"permission_enabled": bool(policy.enabled[Permission.WRITE]), "pending": pending}

    @app.post("/api/skills/write")
    def request_write(body: SkillWriteIn):
        path = body.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="Say which file to write.")
        try:
            mode = classify_write(workspace, path)
        except _FileError as exc:
            result = SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
            return _skill_payload(result, path, "write_file")
        result = skill_executor.submit("write_file", {"path": path, "content": body.content, "mode": mode})
        payload = _skill_payload(result, path, "write_file")
        if result.outcome == "needs_approval":
            payload["mode"] = mode
        return payload

    @app.post("/api/skills/write/confirm")
    def confirm_write(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "write_file")

    @app.post("/api/skills/write/permission")
    def set_write_permission(body: ReadPermissionIn):
        policy.enabled[Permission.WRITE] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="write_file",
            detail="Write File permission enabled" if body.enabled else "Write File permission disabled",
        )
        return {"permission": "write", "enabled": bool(policy.enabled[Permission.WRITE])}

    @app.get("/api/skills/folder")
    def folder_skill_state():
        pending = _visible_pending("create_folder")
        return {"permission_enabled": bool(policy.enabled[Permission.FOLDER]), "pending": pending}

    @app.post("/api/skills/folder")
    def request_folder(body: SkillPathIn):
        path = body.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="Say which folder to create.")
        try:
            prepare_folder(workspace, path)
        except _FileError as exc:
            result = SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
            return _skill_payload(result, path, "create_folder")
        result = skill_executor.submit("create_folder", {"path": path})
        return _skill_payload(result, path, "create_folder")

    @app.post("/api/skills/folder/confirm")
    def confirm_folder(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "create_folder")

    @app.post("/api/skills/folder/permission")
    def set_folder_permission(body: ReadPermissionIn):
        policy.enabled[Permission.FOLDER] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="create_folder",
            detail="Create Folder permission enabled" if body.enabled else "Create Folder permission disabled",
        )
        return {"permission": "folder", "enabled": bool(policy.enabled[Permission.FOLDER])}

    @app.get("/api/skills/system")
    def system_skill_state():
        pending = _visible_pending("system_info")
        return {"permission_enabled": bool(policy.enabled[Permission.SYSTEM]), "pending": pending}

    @app.post("/api/skills/system")
    def request_system(_body: SystemRequestIn):
        result = skill_executor.submit("system_info", {})
        return _skill_payload(result, "", "system_info")

    @app.post("/api/skills/system/confirm")
    def confirm_system(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "system_info")

    @app.post("/api/skills/system/permission")
    def set_system_permission(body: ReadPermissionIn):
        policy.enabled[Permission.SYSTEM] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="system_info",
            detail="System information permission enabled" if body.enabled else "System information permission disabled",
        )
        return {"permission": "system", "enabled": bool(policy.enabled[Permission.SYSTEM])}

    @app.get("/api/skills/apps")
    def apps_skill_state():
        pending = _visible_pending("app_inventory")
        return {"permission_enabled": bool(policy.enabled[Permission.APPS]), "pending": pending}

    @app.post("/api/skills/apps")
    def request_apps(_body: SystemRequestIn):
        result = skill_executor.submit("app_inventory", {})
        return _skill_payload(result, "", "app_inventory")

    @app.post("/api/skills/apps/confirm")
    def confirm_apps(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "app_inventory")

    @app.post("/api/skills/apps/permission")
    def set_apps_permission(body: ReadPermissionIn):
        policy.enabled[Permission.APPS] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="app_inventory",
            detail="Application list permission enabled" if body.enabled else "Application list permission disabled",
        )
        return {"permission": "apps", "enabled": bool(policy.enabled[Permission.APPS])}

    @app.get("/api/skills/clipboard")
    def clipboard_skill_state():
        pending = _visible_pending("clipboard_read")
        return {"permission_enabled": bool(policy.enabled[Permission.CLIPBOARD]), "pending": pending}

    @app.post("/api/skills/clipboard")
    def request_clipboard(_body: SystemRequestIn):
        result = skill_executor.submit("clipboard_read", {})
        return _skill_payload(result, "", "clipboard_read")

    @app.post("/api/skills/clipboard/confirm")
    def confirm_clipboard(body: SkillDecisionIn):
        return _confirm_skill(body.action_id, body.approved, "clipboard_read")

    @app.post("/api/skills/clipboard/permission")
    def set_clipboard_permission(body: ReadPermissionIn):
        policy.enabled[Permission.CLIPBOARD] = bool(body.enabled)
        audit.write(
            status="idle",
            skill_id="clipboard_read",
            detail="Clipboard permission enabled" if body.enabled else "Clipboard permission disabled",
        )
        return {"permission": "clipboard", "enabled": bool(policy.enabled[Permission.CLIPBOARD])}

    @app.post("/api/desktop/plan")
    def desktop_plan(body: DesktopTaskIn):
        if halt.halted:
            raise HTTPException(status_code=409, detail="The assistant is stopped.")
        try:
            plan = propose_desktop_plan(body.task, registry.active(), halt, settings.timeout_s)
        except DesktopPlanError as exc:
            audit.write(status="idle", detail="Desktop plan refused")
            return {"ok": False, "detail": exc.text}
        audit.write(status="idle", detail="Desktop plan prepared")
        return {"ok": True, "plan": plan}

    @app.get("/api/desktop/safety")
    def desktop_safety_status():
        return desktop_safety.report()

    @app.get("/api/desktop/simulator")
    def desktop_simulator_state():
        return desktop_simulator.snapshot()

    @app.post("/api/desktop/simulator/propose")
    def desktop_simulator_propose(body: DesktopSimIn):
        return desktop_simulator.propose(body.model_dump())

    @app.post("/api/desktop/simulator/decide")
    def desktop_simulator_decide(body: SkillDecisionIn):
        return desktop_simulator.decide(body.action_id, body.approved)

    @app.post("/api/desktop/simulator/reset")
    def desktop_simulator_reset(_body: SystemRequestIn):
        result = desktop_simulator.reset()
        audit.write(status="idle", detail="Desktop simulation reset")
        return result

    @app.get("/api/agent/plan")
    def agent_plan():
        return orchestrator.snapshot()

    @app.post("/api/agent/plan")
    def start_agent_plan(body: AgentTaskIn):
        return orchestrator.start(body.task)

    @app.post("/api/agent/plan/confirm")
    def confirm_agent_plan(body: SkillDecisionIn):
        return orchestrator.confirm(body.action_id, body.approved)

    @app.post("/api/agent/plan/cancel")
    def cancel_agent_plan():
        return orchestrator.cancel()

    @app.post("/api/emergency-stop")
    def emergency_stop():
        report = halt.emergency_stop()
        desktop_safety.observe_stop()
        desktop_simulator.observe_stop()
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
                research=body.research,
                researcher=app.state.researcher,
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
