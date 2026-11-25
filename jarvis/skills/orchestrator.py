"""Turn one task into a short plan of existing skills. Each step still needs its own approval."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Policy
from jarvis.skills.base import SkillSpec
from jarvis.skills.executor import SkillExecutor
from jarvis.skills.files import _FileError, classify_write
from jarvis.skills.registry import SkillRegistry
from jarvis.skills.schema import validate_arguments

MAX_PLAN_STEPS = 4
MAX_TASK_CHARS = 800
SUPPORTED_SKILLS = ("list_files", "search_files", "read_file", "write_file", "create_folder")
_FILE_TYPES = frozenset({".txt", ".md"})
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_PLAN_TEXT = "- Define the work.\n- Do the work.\n- Review the result.\n"
_BLOCKED = re.compile(r"\b(powershell|cmd|bash|delete|remove|launch|execute|shell|subprocess)\b", re.I)
_SPLIT = re.compile(
    r"\s+and then\s+|\s+then\s+|\s+and\s+(?=(?:create|write|read|list|search)\b)",
    re.I,
)


class PlanError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text


class SkillOrchestrator:
    def __init__(
        self,
        executor: SkillExecutor,
        registry: SkillRegistry,
        halt: HaltController,
        audit: AuditLog,
        workspace: Path,
        policy: Policy,
        provider,
        timeout_s: float,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._halt = halt
        self._audit = audit
        self._workspace = workspace
        self._policy = policy
        self._provider = provider
        self._timeout_s = timeout_s
        self._plan: dict | None = None
        self._lock = threading.RLock()

    def snapshot(self) -> dict:
        with self._lock:
            self._expire_if_stale()
            if self._plan is None:
                return _view(None)
            return _view(self._plan)

    def current_action_id(self) -> str:
        with self._lock:
            self._expire_if_stale()
            plan = self._plan
            if plan is None or plan["status"] != "awaiting_approval":
                return ""
            return plan["action_id"]

    def start(self, task: str) -> dict:
        with self._lock:
            return self._start(task)

    def confirm(self, action_id: str, approved: bool) -> dict:
        with self._lock:
            return self._confirm(action_id, approved)

    def cancel(self) -> dict:
        with self._lock:
            return self._cancel()

    def _start(self, task: str) -> dict:
        self._expire_if_stale()
        if self._halt.halted:
            return _message(False, "The assistant is stopped.", "cancelled", "cancelled")
        if self._active():
            view = self.snapshot()
            view["ok"] = False
            view["text"] = "A plan is already running."
            view["outcome"] = "rejected"
            return view
        from jarvis.skills.planner import propose_plan

        provider = self._provider() if callable(self._provider) else self._provider
        try:
            steps = propose_plan(task, provider, self._halt, self._timeout_s)
            _validate_steps(self._registry, steps)
        except PlanError as exc:
            status = "cancelled" if exc.text == "The plan was cancelled." else "rejected"
            self._plan = _stored("", [], status, self._halt.capture(), exc.text)
            self._audit.write(
                status="stopped" if status == "cancelled" else "denied",
                outcome=status,
                detail="Plan rejected",
            )
            return _view(self._plan)
        self._plan = _stored(task, steps, "running", self._halt.capture(), "")
        self._audit.write(status="requested", detail="Plan proposed")
        return self._submit_current()

    def _confirm(self, action_id: str, approved: bool) -> dict:
        self._expire_if_stale()
        plan = self._plan
        if plan is None or plan["status"] != "awaiting_approval":
            view = self.snapshot()
            view["ok"] = False
            view["text"] = "That plan is not waiting for approval."
            view["outcome"] = "rejected"
            return view
        if action_id != plan["action_id"]:
            view = self.snapshot()
            view["ok"] = False
            view["text"] = "That action is not the current plan step."
            view["outcome"] = "rejected"
            return view
        if not self._halt.is_current(plan["generation"]):
            plan["status"] = "cancelled"
            plan["action_id"] = ""
            plan["text"] = "The plan was cancelled."
            return _view(plan)
        result = self._executor.confirm(action_id, approved=approved)
        if result.outcome == "disabled":
            self._executor.confirm(action_id, approved=False)
        plan["action_id"] = ""
        step = plan["steps"][plan["index"]]
        plan["results"].append(_result_row(plan["index"], step, result.ok, result.text, result.outcome))
        if not approved or result.outcome == "denied":
            plan["status"] = "denied"
            plan["text"] = "The plan stopped because an action was denied."
            return _view(plan)
        if result.outcome == "cancelled" or not self._halt.is_current(plan["generation"]):
            plan["status"] = "cancelled"
            plan["text"] = "The plan was cancelled."
            return _view(plan)
        if not result.ok:
            plan["status"] = "failed"
            plan["text"] = result.text or "The plan stopped because an action failed."
            return _view(plan)
        plan["index"] += 1
        if plan["index"] >= len(plan["steps"]):
            plan["status"] = "completed"
            plan["text"] = "The plan finished."
            self._audit.write(status="completed", detail="Plan finished")
            return _view(plan)
        return self._submit_current()

    def _cancel(self) -> dict:
        self._expire_if_stale()
        plan = self._plan
        if plan is None or plan["status"] not in {"running", "awaiting_approval"}:
            return self.snapshot()
        action_id = plan["action_id"]
        plan["status"] = "cancelled"
        plan["action_id"] = ""
        plan["text"] = "The plan was cancelled."
        if action_id:
            self._executor.confirm(action_id, approved=False)
        self._audit.write(status="stopped", outcome="cancelled", detail="Plan cancelled")
        return _view(plan)

    def _submit_current(self) -> dict:
        plan = self._plan
        if plan is None:
            return _view(None)
        if not self._halt.is_current(plan["generation"]):
            plan["status"] = "cancelled"
            plan["action_id"] = ""
            plan["text"] = "The plan was cancelled."
            return _view(plan)
        step = plan["steps"][plan["index"]]
        arguments = dict(step["arguments"])
        if step["skill"] == "write_file" and self._skill_enabled(step["skill"]):
            try:
                arguments["mode"] = classify_write(self._workspace, arguments["path"])
            except _FileError as exc:
                plan["status"] = "failed"
                plan["text"] = exc.text
                plan["results"].append(_result_row(plan["index"], step, False, exc.text, "rejected"))
                return _view(plan)
            step["arguments"]["mode"] = arguments["mode"]
            step["summary"] = _write_summary(arguments["path"], arguments["mode"])
        result = self._executor.submit(step["skill"], arguments)
        if result.outcome == "disabled" and step["skill"] == "write_file":
            step["summary"] = "Write file " + arguments["path"]
            step["arguments"]["mode"] = "create"
        if result.outcome == "needs_approval":
            plan["status"] = "awaiting_approval"
            plan["action_id"] = result.action_id
            plan["text"] = "Approve or deny the current action."
            return _view(plan)
        plan["action_id"] = ""
        plan["results"].append(_result_row(plan["index"], step, result.ok, result.text, result.outcome))
        if result.outcome == "cancelled":
            plan["status"] = "cancelled"
            plan["text"] = "The plan was cancelled."
        elif result.outcome == "denied":
            plan["status"] = "denied"
            plan["text"] = result.text or "The plan stopped because an action was denied."
        else:
            plan["status"] = "failed"
            plan["text"] = result.text or "The plan stopped because an action failed."
        return _view(plan)

    def _skill_enabled(self, skill_id: str) -> bool:
        skill = self._registry.get(skill_id)
        spec = getattr(skill, "spec", None)
        return isinstance(spec, SkillSpec) and self._policy.decide(spec) != "disabled"

    def _active(self) -> bool:
        return self._plan is not None and self._plan["status"] in {"running", "awaiting_approval"}

    def _expire_if_stale(self) -> None:
        plan = self._plan
        if plan is None or plan["status"] not in {"running", "awaiting_approval"}:
            return
        if not self._halt.is_current(plan["generation"]):
            plan["status"] = "cancelled"
            plan["action_id"] = ""
            plan["text"] = "The plan was cancelled."


def build_plan(task: object) -> list[dict]:
    if not isinstance(task, str):
        raise PlanError("That task is not a supported plan.")
    text = " ".join(task.split())
    if not text or len(text) > MAX_TASK_CHARS:
        raise PlanError("That task is not a supported plan.")
    if _BLOCKED.search(text):
        raise PlanError("That task uses an unsupported action.")
    clauses = _split_clauses(text)
    if not clauses or len(clauses) > MAX_PLAN_STEPS:
        raise PlanError("That task is not a supported plan.")
    steps: list[dict] = []
    folders: list[str] = []
    for clause in clauses:
        steps.append(_parse_clause(clause, folders))
    _require_known_parents(steps)
    return steps


def _split_clauses(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            buf.append(char)
            index += 1
            continue
        match = _SPLIT.match(text, index)
        if match:
            parts.append("".join(buf))
            buf = []
            index = match.end()
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return [part.strip(" .") for part in parts if part.strip(" .")]


def _parse_clause(clause: str, folders: list[str]) -> dict:
    patterns = (
        (r"create folder (\S+)", _folder_clause),
        (r"create (?:a |an )?(.+?) folder(?: in the workspace)?", _folder_clause),
        (r"create (\S+) inside it with (.+)", _inside_previous),
        (r"create (\S+) inside (\S+) with (.+)", _inside_named),
        (r"(?:write|create) (\S+\.(?:txt|md)) with (.+)", _write_clause),
        (r"read (\S+)", _read_clause),
        (r"list files in (\S+)", _list_clause),
        (r"list files", _list_root),
        (r"list (\S+)", _list_clause),
        (r"search files for (.+)", _search_clause),
        (r"search for (.+)", _search_clause),
    )
    for pattern, parser in patterns:
        match = re.fullmatch(pattern, clause, re.I)
        if match:
            return parser(match, folders)
    raise PlanError("That task is not a supported plan.")


def _folder_clause(match: re.Match, folders: list[str]) -> dict:
    path = _safe_relative(match.group(1), file=False)
    folders.append(path)
    return _step("create_folder", {"path": path}, "Create folder " + path)


def _inside_previous(match: re.Match, folders: list[str]) -> dict:
    if not folders:
        raise PlanError("That task does not say which folder to use.")
    return _write_step(folders[-1] + "/" + _file_name(match.group(1)), match.group(2))


def _inside_named(match: re.Match, folders: list[str]) -> dict:
    folder = _safe_relative(match.group(2), file=False)
    return _write_step(folder + "/" + _file_name(match.group(1)), match.group(3))


def _write_clause(match: re.Match, folders: list[str]) -> dict:
    return _write_step(_safe_relative(match.group(1), file=True), match.group(2))


def _read_clause(match: re.Match, folders: list[str]) -> dict:
    path = _safe_relative(match.group(1), file=True)
    return _step("read_file", {"path": path}, "Read file " + path)


def _list_clause(match: re.Match, folders: list[str]) -> dict:
    path = _safe_relative(match.group(1), file=False)
    return _step("list_files", {"path": path}, "List files in " + path)


def _list_root(match: re.Match, folders: list[str]) -> dict:
    return _step("list_files", {"path": ""}, "List files")


def _search_clause(match: re.Match, folders: list[str]) -> dict:
    query = match.group(1).strip().strip('"').strip("'")
    if not query or len(query) > 80 or any(char in query for char in "\\/:") or ".." in query:
        raise PlanError("That search is not valid.")
    return _step("search_files", {"query": query}, "Search files for " + query)


def _write_step(path: str, content: str) -> dict:
    safe = _safe_relative(path, file=True)
    return _step(
        "write_file",
        {"path": safe, "content": _file_content(content, safe), "mode": "create"},
        _write_summary(safe, "create"),
    )


def _file_name(raw: str) -> str:
    name = _safe_relative(raw, file=True)
    if "/" in name or "\\" in name:
        raise PlanError("That path is outside the workspace.")
    return name


def _file_content(raw: str, path: str) -> str:
    text = raw.strip().strip('"').strip("'").rstrip(".")
    if text.lower() in {"a short project plan", "a short plan", "short project plan"}:
        return Path(path).stem + "\n" + _PLAN_TEXT
    if not text or "\x00" in text or len(text) > 100_000:
        raise PlanError("That file content is not valid.")
    return text


def _safe_relative(raw: str, *, file: bool) -> str:
    text = raw.strip().strip('"').strip("'")
    if not text or any(char in text for char in "\x00\r\n") or ":" in text or text.startswith(("/", "\\")):
        raise PlanError("That path is outside the workspace.")
    parts = Path(text).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PlanError("That path is outside the workspace.")
    for index, part in enumerate(parts):
        if part.endswith((" ", ".")) or any(ord(char) < 32 for char in part) or any(char in part for char in '<>:"|?*'):
            raise PlanError("That path is not valid.")
        file_name = file and index == len(parts) - 1
        if not file_name and part.split(".", 1)[0].upper() in _RESERVED:
            raise PlanError("That path is not valid.")
    if file and Path(text).suffix.lower() not in _FILE_TYPES:
        raise PlanError("Only .txt and .md files can be used.")
    return Path(text).as_posix()


def _validate_steps(registry: SkillRegistry, steps: list[dict]) -> None:
    if not steps or len(steps) > MAX_PLAN_STEPS:
        raise PlanError("That task is not a supported plan.")
    for step in steps:
        skill_id = step["skill"]
        if skill_id not in SUPPORTED_SKILLS:
            raise PlanError("That task uses an unsupported action.")
        skill = registry.get(skill_id)
        spec = getattr(skill, "spec", None)
        if not isinstance(spec, SkillSpec) or spec.id != skill_id:
            raise PlanError("That task uses an unsupported action.")
        try:
            validate_arguments(spec.parameters, step["arguments"])
        except ValueError as exc:
            raise PlanError("That task is not a supported plan.") from exc


def _require_known_parents(steps: list[dict]) -> None:
    created: set[str] = set()
    for step in steps:
        if step["skill"] != "create_folder":
            continue
        path = step["arguments"]["path"]
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent and parent not in created:
            raise PlanError("That folder's parent is not part of this plan.")
        created.add(path)


def _stored(task: object, steps: list[dict], status: str, generation: int, text: str) -> dict:
    shown = " ".join(task.split())[:MAX_TASK_CHARS] if isinstance(task, str) else ""
    return {
        "task": shown,
        "steps": steps,
        "index": 0,
        "results": [],
        "status": status,
        "generation": generation,
        "action_id": "",
        "text": text,
    }


def _step(skill: str, arguments: dict, summary: str) -> dict:
    return {"skill": skill, "arguments": arguments, "summary": summary}


def _write_summary(path: str, mode: str) -> str:
    verb = "Overwrite file" if mode == "overwrite" else "Create file"
    return verb + " " + path


def _result_row(index: int, step: dict, ok: bool, text: str, outcome: str) -> dict:
    return {
        "index": index,
        "skill": step["skill"],
        "summary": step["summary"],
        "ok": ok,
        "text": text,
        "outcome": outcome,
    }


def _message(ok: bool, text: str, outcome: str, status: str) -> dict:
    return {"ok": ok, "text": text, "outcome": outcome, "status": status, "task": "", "steps": [], "current": None, "results": []}


def _view(plan: dict | None) -> dict:
    if plan is None:
        return _message(True, "No plan is running.", "idle", "idle")
    steps = []
    for index, step in enumerate(plan["steps"]):
        steps.append(
            {
                "index": index,
                "skill": step["skill"],
                "summary": step["summary"],
                "path": step["arguments"].get("path", step["arguments"].get("query", "")),
                "state": _step_state(plan, index),
            }
        )
    current = None
    if plan["status"] == "awaiting_approval" and plan["index"] < len(plan["steps"]):
        step = plan["steps"][plan["index"]]
        current = {
            "index": plan["index"],
            "skill": step["skill"],
            "summary": step["summary"],
            "action_id": plan["action_id"],
            "path": step["arguments"].get("path", ""),
            "query": step["arguments"].get("query", ""),
            "mode": step["arguments"].get("mode", ""),
        }
        if step["skill"] == "write_file":
            current["content"] = step["arguments"].get("content", "")
    return {
        "ok": plan["status"] in {"awaiting_approval", "running", "completed", "idle"},
        "text": plan["text"],
        "outcome": plan["status"],
        "status": plan["status"],
        "task": plan["task"],
        "steps": steps,
        "current": current,
        "results": list(plan["results"]),
    }


def _step_state(plan: dict, index: int) -> str:
    for row in plan["results"]:
        if row["index"] == index:
            return "done" if row["ok"] else "stopped"
    if plan["status"] in {"awaiting_approval", "running"} and index == plan["index"]:
        return "current"
    return "not run"
