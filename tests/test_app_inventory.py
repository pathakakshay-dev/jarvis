from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.audit import AuditLog
from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, Policy
from jarvis.skills.apps import AppInventory, clean_display_name, collect_app_inventory, format_app_list
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.orchestrator import SUPPORTED_SKILLS, PlanError
from jarvis.skills.planner import parse_proposed_plan
from jarvis.skills.registry import SkillRegistry

_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


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
    registry.register(AppInventory(halt, reader=wrapped))
    policy = Policy(tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return SkillExecutor(registry, policy, halt, audit), policy, halt, calls


class _FakeRegistry:
    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200
    KEY_SET_VALUE = 0x0002
    KEY_CREATE_SUB_KEY = 0x0004
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, trees, missing=()):
        self.trees = trees
        self.missing = set(missing)
        self.queries = []
        self.opened = []

    def OpenKey(self, hive, path, reserved=0, access=0):
        if isinstance(hive, _FakeKey):
            values = hive.entries.get(path)
            if values is None:
                raise FileNotFoundError(path)
            child = _FakeKey(self, {})
            child.values = values
            return child
        self.opened.append((hive, path, access))
        if (hive, access) in self.missing or path != _PATH:
            raise FileNotFoundError(path)
        if access & self.KEY_SET_VALUE or access & self.KEY_CREATE_SUB_KEY:
            raise OSError("write")
        return _FakeKey(self, self.trees.get((hive, access), {}))

    def QueryInfoKey(self, key):
        return (len(key.entries), 0)

    def EnumKey(self, key, index):
        return list(key.entries)[index]

    def QueryValueEx(self, key, name):
        self.queries.append(name)
        if name != "DisplayName":
            raise OSError(name)
        value = key.values.get("DisplayName")
        if value is None:
            raise FileNotFoundError(name)
        text, kind = value
        return text, kind

    def CloseKey(self, key):
        return None


class _FakeKey:
    def __init__(self, registry, entries):
        self.registry = registry
        self.entries = entries
        self.values = {}


def _reader(trees, missing=()):
    registry = _FakeRegistry(trees, missing)

    def read(halt):
        return collect_app_inventory(halt, registry=registry)

    return read, registry


def _entry(display, kind=1, **extra):
    values = {"DisplayName": (display, kind)}
    values.update(extra)
    return values


def test_display_names_are_deduplicated_and_sorted():
    text = format_app_list(
        [
            "  Zebra Notes  ",
            "alpha mail",
            "Alpha Mail",
            r"C:\Program Files\secret\app.exe",
            "msiexec /x {secret}",
            "Vendor Suite",
        ]
    )
    assert text.splitlines() == ["Installed applications:", "Alpha Mail", "Vendor Suite", "Zebra Notes"]
    assert "secret" not in text
    assert "Program Files" not in text


def test_list_stops_at_one_hundred_names():
    names = [f"App {index:03d}" for index in range(130)]
    text = format_app_list(names)
    lines = text.splitlines()
    assert lines[0] == "Installed applications:"
    assert lines[1] == "App 000"
    assert lines[100] == "App 099"
    assert lines[-1] == "List limited to 100 names."
    assert "App 100" not in text


def test_missing_registry_locations_are_skipped():
    machine = {
        "Alpha": _entry("Alpha"),
        "Hidden": _entry(r"C:\Users\secret\app.exe"),
        "Blank": _entry("   "),
        "Expanded": _entry("%ProgramFiles%\\Vendor", kind=2),
        "Quiet": {
            "DisplayName": ("Kept", 1),
            "UninstallString": (r"MsiExec.exe /X{SECRET-KEY}", 1),
            "InstallLocation": (r"C:\Program Files\Vendor", 1),
        },
    }
    trees = {
        (1, 0x20019 | 0x0100): machine,
        (1, 0x20019 | 0x0200): {"Alpha": _entry(" alpha ")},
    }
    read, registry = _reader(trees, missing={(2, 0x20019)})
    text = read(HaltController())
    assert text.splitlines() == ["Installed applications:", "Alpha", "Kept"]
    assert registry.queries == ["DisplayName", "DisplayName", "DisplayName", "DisplayName", "DisplayName", "DisplayName"]
    assert "SECRET" not in text
    assert "InstallLocation" not in text
    assert "UninstallString" not in text


def test_every_missing_location_fails_safely():
    read, _registry = _reader({}, missing={(1, 0x20019 | 0x0100), (1, 0x20019 | 0x0200), (2, 0x20019)})
    text = read(HaltController())
    assert text == "Installed applications are unavailable."
    assert clean_display_name("Notepad") == "Notepad"


def test_permission_approval_and_denial(tmp_path: Path):
    executor, policy, _halt, calls = _executor(tmp_path, lambda _halt: "Installed applications:\nNotepad")
    refused = executor.submit("app_inventory", {})
    assert refused.outcome == "disabled"
    assert calls == []
    policy.enabled[Permission.APPS] = True
    waiting = executor.submit("app_inventory", {})
    assert waiting.outcome == "needs_approval"
    denied = executor.confirm(waiting.action_id, approved=False)
    assert denied.outcome == "denied"
    assert calls == []
    waiting = executor.submit("app_inventory", {})
    approved = executor.confirm(waiting.action_id, approved=True)
    assert approved.text == "Installed applications:\nNotepad"
    assert calls == [False]
    replay = executor.confirm(waiting.action_id, approved=True)
    assert replay.error_type == "unknown_action"
    forged = executor.confirm("not-a-real-action", approved=True)
    assert forged.error_type == "unknown_action"
    assert calls == [False]
    extra = executor.submit("app_inventory", {"command": "powershell"})
    assert extra.error_type == "invalid_arguments"


def test_stop_discards_the_list_and_resume_does_not_replay(tmp_path: Path):
    def reader(active):
        active.emergency_stop()
        return "Installed applications:\nSECRET-APP"

    executor, policy, halt, calls = _executor(tmp_path, reader)
    policy.enabled[Permission.APPS] = True
    waiting = executor.submit("app_inventory", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.outcome == "cancelled"
    assert result.text == ""
    assert "SECRET-APP" not in (result.text or "")
    halt.resume()
    replay = executor.confirm(waiting.action_id, approved=True)
    assert replay.error_type == "unknown_action"
    assert calls == [False]


def test_reader_failure_hides_the_exception(tmp_path: Path):
    def reader(_active):
        raise OSError(r"C:\Users\secret\NTUSER.DAT")

    executor, policy, _halt, _calls = _executor(tmp_path, reader)
    policy.enabled[Permission.APPS] = True
    waiting = executor.submit("app_inventory", {})
    result = executor.confirm(waiting.action_id, approved=True)
    assert result.text == "Installed applications are unavailable."
    assert "secret" not in result.text.lower()


def test_apps_api_stays_separate_and_rejects_forged_approval(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), fake=FakeProvider(reply="local reply"), start_hotkeys_flag=False)
    with TestClient(app) as client:
        assert client.get("/api/status").json()["permissions"]["apps"] is False
        page = client.get("/")
        assert 'id="apps-result"' in page.text
        assert "Applications is off." in page.text
        refused = client.post("/api/skills/apps", json={})
        assert refused.json()["outcome"] == "disabled"
        assert "Installed applications:" not in refused.json()["text"]
        assert client.post("/api/skills/apps", json={"launch": "notepad"}).status_code == 422
        enabled = client.post("/api/skills/apps/permission", json={"enabled": True})
        assert enabled.json() == {"permission": "apps", "enabled": True}
        permissions = client.get("/api/status").json()["permissions"]
        assert permissions["apps"] is True
        assert permissions["system"] is False
        assert permissions["read"] is False
        assert permissions["write"] is False
        assert permissions["app_control"] is False
        waiting = client.post("/api/skills/apps", json={})
        action_id = waiting.json()["action_id"]
        stolen = client.post("/api/skills/system/confirm", json={"action_id": action_id, "approved": True})
        assert stolen.json()["error_type"] == "unknown_action"
        forged = client.post("/api/skills/apps/confirm", json={"action_id": "forged", "approved": True})
        assert forged.json()["error_type"] == "unknown_action"
        denied = client.post("/api/skills/apps/confirm", json={"action_id": action_id, "approved": False})
        assert denied.json()["outcome"] == "denied"
        again = client.post("/api/skills/apps", json={})
        client.post("/api/emergency-stop")
        client.post("/api/resume")
        replay = client.post("/api/skills/apps/confirm", json={"action_id": again.json()["action_id"], "approved": True})
        assert replay.json()["outcome"] == "cancelled"
        assert replay.json()["text"] == ""


def test_planner_cannot_select_app_inventory():
    assert "app_inventory" not in SUPPORTED_SKILLS
    try:
        parse_proposed_plan('{"steps":[{"skill":"app_inventory","arguments":{}}]}')
    except PlanError as exc:
        assert "unsupported" in str(exc).lower()
    else:
        raise AssertionError("application inventory was accepted as a plan step")


def test_app_inventory_source_is_read_only():
    source = Path("jarvis/skills/apps.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for blocked in ("subprocess", "powershell", "os.system", "uninstallstring", "installlocation", "setvalue", "deletekey", "createkey"):
        assert blocked not in lowered
    assert _PATH in source
