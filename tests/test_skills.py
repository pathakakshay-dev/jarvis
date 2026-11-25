from pathlib import Path

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.base import SkillResult, SkillSpec
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.registry import SkillRegistry

NOTE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 40}},
    "required": ["path"],
    "additionalProperties": False,
}


class RecordingSkill:
    def __init__(self, spec: SkillSpec, halt: HaltController | None = None, fail: bool = False) -> None:
        self.spec = spec
        self.halt = halt
        self.fail = fail
        self.calls: list[dict] = []

    def execute(self, arguments: dict) -> SkillResult:
        self.calls.append(dict(arguments))
        if self.fail:
            raise RuntimeError("boom")
        if self.halt is not None:
            self.halt.emergency_stop()
            return SkillResult(True, text="should be discarded", outcome="completed")
        return SkillResult(True, text="done", outcome="completed")


def _stack(tmp_path: Path) -> tuple[SkillRegistry, Policy, HaltController, AuditLog, SkillExecutor]:
    registry = SkillRegistry()
    policy = Policy(tmp_path)
    halt = HaltController()
    audit = AuditLog(tmp_path / "audit.jsonl")
    return registry, policy, halt, audit, SkillExecutor(registry, policy, halt, audit)


def _statuses(audit: AuditLog) -> list[str]:
    return [row["status"] for row in audit.tail(20)]


def test_registered_skill_stays_disabled_until_policy_allows_it(tmp_path: Path):
    registry, policy, halt, audit, executor = _stack(tmp_path)
    skill = RecordingSkill(SkillSpec("lookup", "read a note", Permission.READ, NOTE_SCHEMA))
    registry.register(skill)
    assert executor.catalog() == [
        {
            "name": "lookup",
            "description": "read a note",
            "permission": "read",
            "input_schema": NOTE_SCHEMA,
            "enabled": False,
        }
    ]
    refused = executor.submit("lookup", {"path": "note.txt"})
    assert refused.outcome == "disabled"
    assert skill.calls == []
    policy.enabled[Permission.READ] = True
    assert executor.catalog()[0]["enabled"] is True
    done = executor.submit("lookup", {"path": "note.txt"})
    assert done.ok is True
    assert done.text == "done"
    assert skill.calls == [{"path": "note.txt"}]
    assert "requested" in _statuses(audit)
    assert "denied" in _statuses(audit)
    assert "executed" in _statuses(audit)


def test_unknown_shell_and_invalid_arguments_are_rejected(tmp_path: Path):
    registry, policy, halt, audit, executor = _stack(tmp_path)
    skill = RecordingSkill(SkillSpec("lookup", "read a note", Permission.READ, NOTE_SCHEMA))
    registry.register(skill)
    policy.enabled[Permission.READ] = True
    unknown = executor.submit("python", {"code": "print(1)"})
    shell = executor.submit("powershell", {"path": "note.txt"})
    extra = executor.submit("lookup", {"path": "note.txt", "command": "dir"})
    missing = executor.submit("lookup", {})
    wrong = executor.submit("lookup", {"path": 12})
    assert unknown.error_type == "unknown_skill"
    assert shell.error_type == "shell_disabled"
    assert extra.error_type == "invalid_arguments"
    assert missing.error_type == "invalid_arguments"
    assert wrong.error_type == "invalid_arguments"
    assert skill.calls == []
    assert "subprocess" not in Path("jarvis/skills/executor.py").read_text(encoding="utf-8")
    assert "os.system" not in Path("jarvis/skills/executor.py").read_text(encoding="utf-8")


def test_sensitive_approval_is_one_action_and_resume_does_not_replay(tmp_path: Path):
    registry, policy, halt, audit, executor = _stack(tmp_path)
    skill = RecordingSkill(SkillSpec("save", "write a note", Permission.WRITE, NOTE_SCHEMA))
    registry.register(skill)
    policy.enabled[Permission.WRITE] = True
    first = executor.submit("save", {"path": "a.txt"})
    second = executor.submit("save", {"path": "b.txt"})
    assert first.outcome == "needs_approval"
    assert second.outcome == "needs_approval"
    assert first.action_id != second.action_id
    assert skill.calls == []
    denied = executor.confirm(second.action_id, approved=False)
    assert denied.outcome == "denied"
    assert skill.calls == []
    accepted = executor.confirm(first.action_id, approved=True)
    assert accepted.ok is True
    assert skill.calls == [{"path": "a.txt"}]
    again = executor.confirm(first.action_id, approved=True)
    assert again.error_type == "unknown_action"
    assert skill.calls == [{"path": "a.txt"}]
    waiting = executor.submit("save", {"path": "c.txt"})
    halt.emergency_stop()
    halt.resume()
    replayed = executor.confirm(waiting.action_id, approved=True)
    assert replayed.outcome == "cancelled"
    assert skill.calls == [{"path": "a.txt"}]
    assert {"requesting_approval", "approved", "denied", "stopped"} <= set(_statuses(audit))


def test_stop_blocks_execution_and_discards_a_late_result(tmp_path: Path):
    registry, policy, halt, audit, executor = _stack(tmp_path)
    skill = RecordingSkill(SkillSpec("lookup", "read a note", Permission.READ, NOTE_SCHEMA), halt=halt)
    registry.register(skill)
    policy.enabled[Permission.READ] = True
    halt.emergency_stop()
    blocked = executor.submit("lookup", {"path": "note.txt"})
    assert blocked.outcome == "cancelled"
    assert skill.calls == []
    halt.resume()
    discarded = executor.submit("lookup", {"path": "note.txt"})
    assert discarded.outcome == "cancelled"
    assert discarded.text == ""
    assert skill.calls == [{"path": "note.txt"}]
    halt.resume()
    failing = RecordingSkill(SkillSpec("broken", "fails", Permission.READ, NOTE_SCHEMA), fail=True)
    registry.register(failing)
    failed = executor.submit("broken", {"path": "note.txt"})
    assert failed.outcome == "error"
    assert failed.text == "The skill failed."
    assert "error" in _statuses(audit)


def test_app_keeps_research_off_the_executor(tmp_path: Path):
    app = create_app(
        settings=Settings(
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
        ),
        fake=FakeProvider(reply="local reply"),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
    )
    catalog = app.state.skill_executor.catalog()
    assert [row["name"] for row in catalog] == ["web_research", "read_file", "list_files", "search_files", "write_file", "create_folder", "system_info", "app_inventory", "clipboard_read"]
    assert all(row["enabled"] is False for row in catalog)
    assert catalog[1]["permission"] == "read"
    assert catalog[2]["permission"] == "list"
    assert catalog[3]["permission"] == "search"
    assert catalog[4]["permission"] == "write"
    assert catalog[5]["permission"] == "folder"
    assert catalog[5]["enabled"] is False
    assert "path" in catalog[1]["input_schema"]["properties"]
    refused = app.state.skill_executor.submit("web_research", {})
    assert refused.outcome == "disabled"
    assert app.state.researcher.search_calls == 0
    app.state.policy.enabled[Permission.READ] = True
    still = app.state.skill_executor.submit("web_research", {})
    assert still.error_type == "not_executable"
    assert app.state.researcher.search_calls == 0
    pending = app.state.skill_executor.submit("read_file", {"path": "note.txt"})
    assert pending.outcome == "needs_approval"
    assert pending.text != "secret"
