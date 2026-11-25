from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.clipboard import ClipboardRead, limit_clipboard_text, read_clipboard_text
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.orchestrator import SUPPORTED_SKILLS, PlanError
from jarvis.skills.planner import parse_proposed_plan
from jarvis.skills.registry import SkillRegistry

_SECRET = "otp-991122"


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


def _executor(tmp_path: Path, reader):
    halt = HaltController()
    calls = []

    def wrapped(active):
        calls.append(active.halted)
        return reader(active)

    registry = SkillRegistry()
    registry.register(ClipboardRead(halt, reader=wrapped))
    policy = Policy(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return SkillExecutor(registry, policy, halt, audit), policy, halt, calls


class _Board:
    def __init__(self, text=None, available=True, open_ok=True, lock_ok=True):
        self.text = text
        self.available = available
        self.open_ok = open_ok
        self.lock_ok = lock_ok
        self.formats = []
        self.closed = False
        self.unlocked = False

    def OpenClipboard(self, hwnd):
        return self.open_ok

    def CloseClipboard(self):
        self.closed = True
        return True

    def IsClipboardFormatAvailable(self, fmt):
        self.formats.append(fmt)
        return self.available

    def GetClipboardData(self, fmt):
        self.formats.append(fmt)
        if fmt != 13 or self.text is None:
            return 0
        return 7

    def GlobalLock(self, handle):
        return 7 if self.lock_ok else 0

    def GlobalUnlock(self, handle):
        self.unlocked = True
        return True


def _read(board, halt=None):
    return read_clipboard_text(halt or HaltController(), user32=board, kernel32=board, copy_text=lambda _pointer: board.text)


def test_clipboard_text_is_returned_once_and_closed():
    board = _Board("hello clipboard")
    assert _read(board) == "hello clipboard"
    assert board.formats == [13, 13]
    assert board.closed is True
    assert board.unlocked is True


def test_empty_non_text_and_unavailable_clipboard():
    assert _read(_Board("")) == "The clipboard is empty."
    board = _Board(available=False)
    assert _read(board) == "The clipboard has no text."
    assert board.formats == [13]
    assert board.closed is True
    assert _read(_Board(open_ok=False)) == "The clipboard is unavailable."
    assert _read(_Board("text", lock_ok=False)) == "The clipboard is unavailable."


def test_clipboard_text_is_limited_to_5000_characters():
    assert len(limit_clipboard_text("x" * 5000)) == 5000
    limited = limit_clipboard_text("y" * 5001)
    assert limited.startswith("y" * 5000)
    assert limited.endswith("Showing the first 5000 characters.")
    assert "y" * 5001 not in limited
    board = _Board("z" * 6000)
    text = _read(board)
    assert text.startswith("z" * 5000)
    assert "z" * 5001 not in text


def test_permission_approval_denial_and_forged_id(tmp_path: Path):
    executor, policy, _halt, calls = _executor(tmp_path, lambda _halt: _SECRET)
    refused = executor.submit("clipboard_read", {})
    assert refused.outcome == "disabled"
    assert calls == []
    policy.enabled[Permission.CLIPBOARD] = True
    waiting = executor.submit("clipboard_read", {})
    denied = executor.confirm(waiting.action_id, approved=False)
    assert denied.outcome == "denied"
    assert calls == []
    waiting = executor.submit("clipboard_read", {})
    approved = executor.confirm(waiting.action_id, approved=True)
    assert approved.text == _SECRET
    assert calls == [False]
    assert executor.confirm(waiting.action_id, approved=True).error_type == "unknown_action"
    assert executor.confirm("forged", approved=True).error_type == "unknown_action"
    assert executor.submit("clipboard_read", {"write": "pasted"}).error_type == "invalid_arguments"
    assert calls == [False]
    assert _SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_stop_discards_clipboard_text_and_resume_does_not_replay(tmp_path: Path):
    def reader(active):
        active.emergency_stop()
        return _SECRET

    executor, policy, halt, calls = _executor(tmp_path, reader)
    policy.enabled[Permission.CLIPBOARD] = True
    waiting = executor.submit("clipboard_read", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.outcome == "cancelled"
    assert result.text == ""
    halt.resume()
    replay = executor.confirm(waiting.action_id, approved=True)
    assert replay.error_type == "unknown_action"
    assert calls == [False]
    assert _SECRET not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_clipboard_failure_hides_the_exception(tmp_path: Path):
    def reader(_active):
        raise OSError(r"C:\Users\secret\clipboard")

    executor, policy, _halt, _calls = _executor(tmp_path, reader)
    policy.enabled[Permission.CLIPBOARD] = True
    waiting = executor.submit("clipboard_read", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.text == "The clipboard is unavailable."
    assert "secret" not in result.text.lower()


def test_clipboard_content_never_reaches_provider_memory_audit_or_chat(tmp_path: Path):
    fake = FakeProvider(reply="local reply")
    app = create_app(settings=_settings(tmp_path), fake=fake, start_hotkeys_flag=False)
    app.state.skills.get("clipboard_read")._reader = lambda _halt: _SECRET
    with TestClient(app) as client:
        assert client.get("/api/status").json()["permissions"]["clipboard"] is False
        page = client.get("/")
        assert "Clipboard may contain passwords, OTPs, tokens or private information." in page.text
        assert client.post("/api/skills/clipboard", json={}).json()["outcome"] == "disabled"
        client.post("/api/skills/clipboard/permission", json={"enabled": True})
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["clipboard"] is True
        assert permissions["read"] is False
        assert permissions["apps"] is False
        waiting = client.post("/api/skills/clipboard", json={})
        action_id = waiting.json()["action_id"]
        assert _SECRET not in waiting.text
        stolen = client.post("/api/agent/plan/confirm", json={"action_id": action_id, "approved": True})
        assert _SECRET not in stolen.text
        forged = client.post("/api/skills/clipboard/confirm", json={"action_id": "forged", "approved": True})
        assert forged.json()["error_type"] == "unknown_action"
        approved = client.post("/api/skills/clipboard/confirm", json={"action_id": action_id, "approved": True})
        assert approved.json()["text"] == _SECRET
        status = client.get("/api/status")
        listed = client.get("/api/skills/clipboard")
        messages = client.get("/api/messages")
        memories = client.get("/api/memories")
        plan = client.get("/api/agent/plan")
        for response in (status, listed, messages, memories, plan):
            assert _SECRET not in response.text
        chat = client.post("/api/chat", json={"text": "hello"})
        assert chat.status_code == 200
        assert _SECRET not in chat.text
        seen = "\n".join(str(message) for batch in fake.messages_seen for message in batch)
        assert _SECRET not in seen
        assert fake.calls == 1
    audit = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
    database = (tmp_path / "data" / "jarvis.sqlite3").read_bytes()
    assert _SECRET not in audit
    assert _SECRET.encode() not in database


def test_planner_cannot_select_clipboard_read():
    assert "clipboard_read" not in SUPPORTED_SKILLS
    try:
        parse_proposed_plan('{"steps":[{"skill":"clipboard_read","arguments":{}}]}')
    except PlanError as exc:
        assert "unsupported" in str(exc).lower()
    else:
        raise AssertionError("clipboard read was accepted as a plan step")


def test_clipboard_skill_does_not_write_or_run_commands():
    source = Path("jarvis/skills/clipboard.py").read_text(encoding="utf-8").lower()
    for blocked in ("subprocess", "powershell", "os.system", "setclipboarddata", "emptyclipboard"):
        assert blocked not in source
