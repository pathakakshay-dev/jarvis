"""Read, list, search, or write inside the workspace. File text is data, never code to run."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from jarvis.safety.halt import HaltController
from jarvis.safety.policy import Permission, resolve_inside
from jarvis.skills.base import SkillResult, SkillSpec

MAX_FILE_BYTES = 100_000
MAX_LIST_ENTRIES = 100
MAX_SEARCH_DEPTH = 4
MAX_SEARCH_RESULTS = 40
MAX_SEARCH_VISITS = 300
_ALLOWED = frozenset({".txt", ".md"})

_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 240}},
    "required": ["path"],
    "additionalProperties": False,
}


class _FileError(Exception):
    def __init__(self, text: str, error_type: str) -> None:
        self.text = text
        self.error_type = error_type


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _path_has_link(workspace: Path, raw: str) -> bool:
    current = workspace.resolve()
    for part in Path(raw).parts:
        if part in {"", ".", ".."}:
            return True
        current = current / part
        if _is_reparse(current):
            return True
    return False


class ReadFile:
    spec = SkillSpec(
        id="read_file",
        description="Read one .txt or .md file from the workspace.",
        permission=Permission.READ,
        parameters=_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, workspace: Path, halt: HaltController) -> None:
        self.workspace = workspace
        self._halt = halt
        self.workspace.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = read_workspace_text(self.workspace, str(arguments.get("path", "")))
        except _FileError as exc:
            return SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def read_workspace_text(workspace: Path, raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    if ":" in Path(raw).name:
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    requested = Path(raw.strip()).suffix.lower()
    if requested not in _ALLOWED:
        raise _FileError("Only .txt and .md files can be read.", "unsupported_type")
    if _path_has_link(workspace, raw.strip()):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    try:
        resolved = resolve_inside(workspace, raw.strip())
    except PermissionError as exc:
        raise _FileError("That file is outside the workspace.", "outside_workspace") from exc
    if resolved.suffix.lower() not in _ALLOWED:
        raise _FileError("Only .txt and .md files can be read.", "unsupported_type")
    if resolved.is_dir():
        raise _FileError("That path is not a file.", "not_a_file")
    if not resolved.is_file():
        raise _FileError("That file was not found.", "not_found")
    try:
        if resolved.stat().st_nlink > 1:
            raise _FileError("That file is outside the workspace.", "outside_workspace")
    except _FileError:
        raise
    except OSError as exc:
        raise _FileError("That file could not be read.", "read_error") from exc
    try:
        with resolved.open("rb") as handle:
            payload = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise _FileError("That file could not be read.", "read_error") from exc
    if len(payload) > MAX_FILE_BYTES:
        raise _FileError("That file is too large to read.", "too_large")
    return payload.decode("utf-8", errors="replace")


_LIST_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 240}},
    "required": ["path"],
    "additionalProperties": False,
}


class ListFiles:
    spec = SkillSpec(
        id="list_files",
        description="List the immediate contents of one workspace folder.",
        permission=Permission.LIST,
        parameters=_LIST_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, workspace: Path, halt: HaltController) -> None:
        self.workspace = workspace
        self._halt = halt
        self.workspace.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = list_workspace(self.workspace, str(arguments.get("path", "")))
        except _FileError as exc:
            return SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def list_workspace(workspace: Path, raw: str) -> str:
    if not isinstance(raw, str) or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    text = raw.strip()
    if text in {"", "."}:
        relative = "."
    else:
        if ":" in text or text.startswith("/") or text.startswith("\\"):
            raise _FileError("That folder is outside the workspace.", "outside_workspace")
        if ".." in Path(text).parts:
            raise _FileError("That folder is outside the workspace.", "outside_workspace")
        relative = text
        if _path_has_link(workspace, relative):
            raise _FileError("That folder is outside the workspace.", "outside_workspace")
    try:
        resolved = resolve_inside(workspace, relative)
    except PermissionError as exc:
        raise _FileError("That folder is outside the workspace.", "outside_workspace") from exc
    if not resolved.exists():
        raise _FileError("That folder was not found.", "not_found")
    if not resolved.is_dir():
        raise _FileError("That path is not a folder.", "not_a_folder")
    try:
        children = list(resolved.iterdir())
    except OSError as exc:
        raise _FileError("That folder could not be listed.", "list_error") from exc
    children.sort(key=lambda item: item.name.lower())
    lines = [_list_name(item) for item in children[:MAX_LIST_ENTRIES]]
    if len(children) > MAX_LIST_ENTRIES:
        lines.append("More items were not shown.")
    if not lines:
        return "(empty)"
    return "\n".join(lines)


def _list_name(path: Path) -> str:
    name = path.name.replace("\r", " ").replace("\n", " ")
    try:
        folder = path.is_dir(follow_symlinks=False) and not _is_reparse(path)
    except OSError:
        folder = False
    return name + ("/" if folder else "")


class _Stopped(Exception):
    pass


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 80}},
    "required": ["query"],
    "additionalProperties": False,
}


class SearchFiles:
    spec = SkillSpec(
        id="search_files",
        description="Search filenames inside the workspace.",
        permission=Permission.SEARCH,
        parameters=_SEARCH_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, workspace: Path, halt: HaltController) -> None:
        self.workspace = workspace
        self._halt = halt
        self.workspace.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = search_workspace(self.workspace, arguments.get("query", ""), self._halt)
        except _Stopped:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        except _FileError as exc:
            return SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def search_workspace(workspace: Path, raw: object, halt: HaltController) -> str:
    query = _search_query(raw)
    root = workspace.resolve()
    matches: list[str] = []
    visits = 0
    capped = False
    depth_limited = False
    seen: set[str] = set()

    def walk(directory: Path, depth: int) -> None:
        nonlocal visits, capped, depth_limited
        if halt.halted:
            raise _Stopped()
        try:
            resolved_dir = directory.resolve()
            resolved_dir.relative_to(root)
        except (OSError, ValueError) as exc:
            raise _FileError("That folder is outside the workspace.", "outside_workspace") from exc
        key = os.path.normcase(str(resolved_dir))
        if key in seen:
            return
        seen.add(key)
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            if depth == 0:
                raise _FileError("That folder could not be searched.", "search_error") from exc
            return
        entries.sort(key=lambda entry: entry.name.lower())
        for entry in entries:
            if halt.halted:
                raise _Stopped()
            if visits >= MAX_SEARCH_VISITS or len(matches) >= MAX_SEARCH_RESULTS:
                capped = True
                return
            visits += 1
            if _is_reparse(Path(entry.path)) or _points_outside(root, Path(entry.path)):
                continue
            name = _safe_filename(entry.name)
            if query.casefold() in name.casefold():
                relative = _relative_name(root, Path(entry.path), entry)
                if relative is not None:
                    matches.append(relative)
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        capped = True
                        return
            if depth >= MAX_SEARCH_DEPTH:
                try:
                    if entry.is_dir(follow_symlinks=False) and not _is_reparse(Path(entry.path)):
                        depth_limited = True
                except OSError:
                    pass
                continue
            try:
                if _is_reparse(Path(entry.path)) or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            walk(Path(entry.path), depth + 1)
            if capped:
                return

    walk(root, 0)
    if depth_limited or capped:
        matches.append("More items were not shown.")
    if not matches:
        return "(no matches)"
    return "\n".join(matches)


def _search_query(raw: object) -> str:
    if not isinstance(raw, str) or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _FileError("That search is not valid.", "invalid_query")
    text = raw.strip()
    if not text or len(text) > 80 or text in {".", ".."}:
        raise _FileError("That search is not valid.", "invalid_query")
    if any(ord(char) < 32 for char in text):
        raise _FileError("That search is not valid.", "invalid_query")
    if ":" in text or "/" in text or "\\" in text or ".." in text:
        raise _FileError("That search is not valid.", "invalid_query")
    return text


def _points_outside(root: Path, child: Path) -> bool:
    try:
        resolved = child.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return True
    return False


def _safe_filename(name: str) -> str:
    return "".join(" " if ord(char) < 32 else char for char in name)


def _relative_name(root: Path, child: Path, entry: os.DirEntry) -> str | None:
    try:
        relative = child.relative_to(root).as_posix()
    except ValueError:
        return None
    parts = relative.split("/")
    if not relative or ":" in relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return None
    try:
        folder = entry.is_dir(follow_symlinks=False)
    except OSError:
        folder = False
    safe = "/".join(_safe_filename(part) for part in relative.split("/"))
    return safe + ("/" if folder else "")


_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 240},
        "content": {"type": "string", "maxLength": 100_000},
        "mode": {"type": "string", "maxLength": 16},
    },
    "required": ["path", "content", "mode"],
    "additionalProperties": False,
}


class WriteFile:
    spec = SkillSpec(
        id="write_file",
        description="Create or replace one .txt or .md file in the workspace.",
        permission=Permission.WRITE,
        parameters=_WRITE_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, workspace: Path, halt: HaltController) -> None:
        self.workspace = workspace
        self._halt = halt
        self.workspace.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = write_workspace_text(
                self.workspace,
                arguments.get("path", ""),
                arguments.get("content", ""),
                arguments.get("mode", ""),
                self._halt,
            )
        except _Stopped:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        except _FileError as exc:
            return SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def classify_write(workspace: Path, raw: object) -> str:
    target = _locate_write(workspace, raw)
    if target.is_file():
        return "overwrite"
    return "create"


def write_workspace_text(workspace: Path, raw: object, content: object, mode: object, halt: HaltController) -> str:
    if halt.halted:
        raise _Stopped()
    target = _locate_write(workspace, raw)
    data = _encode_content(content)
    if mode not in {"create", "overwrite"}:
        raise _FileError("That write is not valid.", "invalid_write")
    exists = target.is_file()
    if mode == "create" and exists:
        raise _FileError("That file already exists. Approve an overwrite to replace it.", "already_exists")
    if mode == "overwrite" and not exists:
        raise _FileError("That file is no longer there.", "not_found")
    if halt.halted or _is_reparse(target):
        if halt.halted:
            raise _Stopped()
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    if mode == "create":
        _create_file(target, data)
        if halt.halted:
            _remove_written(target)
            raise _Stopped()
        verb = "Created"
    else:
        _replace_file(target, data, halt)
        verb = "Updated"
    return verb + " " + _display_path(raw) + "."


def _create_file(target: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags)
    except FileExistsError as exc:
        raise _FileError("That file already exists. Approve an overwrite to replace it.", "already_exists") from exc
    except OSError as exc:
        raise _FileError("That file could not be written.", "write_error") from exc
    try:
        _write_all(descriptor, data)
    except Exception:
        os.close(descriptor)
        _remove_written(target)
        raise
    os.close(descriptor)


def _replace_file(target: Path, data: bytes, halt: HaltController) -> None:
    temporary = target.with_name("." + target.stem + "-" + uuid.uuid4().hex + target.suffix)
    if temporary.suffix.lower() not in _ALLOWED or _is_reparse(temporary):
        raise _FileError("That file could not be written.", "write_error")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags)
    except OSError as exc:
        raise _FileError("That file could not be written.", "write_error") from exc
    try:
        _write_all(descriptor, data)
    except Exception:
        os.close(descriptor)
        _remove_written(temporary)
        raise
    os.close(descriptor)
    if halt.halted or _is_reparse(target) or not target.is_file():
        _remove_written(temporary)
        if halt.halted:
            raise _Stopped()
        if _is_reparse(target):
            raise _FileError("That file is outside the workspace.", "outside_workspace")
        raise _FileError("That file is no longer there.", "not_found")
    try:
        os.replace(temporary, target)
    except OSError as exc:
        _remove_written(temporary)
        raise _FileError("That file could not be written.", "write_error") from exc


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    try:
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise _FileError("That file could not be written.", "write_error")
            view = view[written:]
    except OSError as exc:
        raise _FileError("That file could not be written.", "write_error") from exc


def _remove_written(path: Path) -> None:
    try:
        if _is_reparse(path) or not path.exists():
            return
        path.unlink()
    except OSError:
        return


def _locate_write(workspace: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    text = raw.strip()
    if ":" in text or text.startswith("/") or text.startswith("\\"):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    if path.suffix.lower() not in _ALLOWED:
        raise _FileError("Only .txt and .md files can be written.", "unsupported_type")
    root = workspace.resolve()
    current = root
    for part in path.parts[:-1]:
        current = current / part
        _reject_link(root, current, missing_is_error=True)
    target = current / path.parts[-1]
    if _is_reparse(target):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    if target.exists() and not target.is_file():
        raise _FileError("That path is not a file.", "not_a_file")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _FileError("That file is outside the workspace.", "outside_workspace") from exc
    return target


def _reject_link(root: Path, current: Path, missing_is_error: bool) -> None:
    if _is_reparse(current):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    if not current.exists():
        if missing_is_error:
            raise _FileError("That folder was not found.", "not_found")
        return
    if not current.is_dir():
        raise _FileError("That folder was not found.", "not_found")
    try:
        resolved = current.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _FileError("That file is outside the workspace.", "outside_workspace") from exc


def _encode_content(content: object) -> bytes:
    if not isinstance(content, str) or "\x00" in content:
        raise _FileError("That content cannot be written.", "invalid_content")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise _FileError("That content is too large to write.", "too_large")
    return data


def _display_path(raw: object) -> str:
    text = str(raw).strip().replace("\\", "/")
    parts = text.split("/")
    if not text or ":" in text or text.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise _FileError("That file is outside the workspace.", "outside_workspace")
    return text


_FOLDER_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string", "maxLength": 240}},
    "required": ["path"],
    "additionalProperties": False,
}
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class CreateFolder:
    spec = SkillSpec(
        id="create_folder",
        description="Create one new folder inside the workspace.",
        permission=Permission.FOLDER,
        parameters=_FOLDER_SCHEMA,
        requires_approval=True,
    )

    def __init__(self, workspace: Path, halt: HaltController) -> None:
        self.workspace = workspace
        self._halt = halt
        self.workspace.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict) -> SkillResult:
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        try:
            text = create_workspace_folder(self.workspace, arguments.get("path", ""), self._halt)
        except _Stopped:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        except _FileError as exc:
            return SkillResult(False, text=exc.text, outcome="rejected", error_type=exc.error_type)
        if self._halt.halted:
            return SkillResult(False, outcome="cancelled", error_type="stopped")
        return SkillResult(True, text=text, outcome="completed")


def prepare_folder(workspace: Path, raw: object) -> Path:
    return _locate_new_folder(workspace, raw)


def create_workspace_folder(workspace: Path, raw: object, halt: HaltController) -> str:
    if halt.halted:
        raise _Stopped()
    target = _locate_new_folder(workspace, raw)
    if halt.halted:
        raise _Stopped()
    target = _locate_new_folder(workspace, raw)
    try:
        os.mkdir(target)
    except FileExistsError as exc:
        _reject_existing_folder(target)
        raise _FileError("That folder already exists.", "already_exists") from exc
    except OSError as exc:
        raise _FileError("That folder could not be created.", "folder_error") from exc
    return "Created " + _display_path(raw) + "."


def _locate_new_folder(workspace: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    text = raw.strip()
    if ":" in text or text.startswith("/") or text.startswith("\\"):
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    for part in path.parts:
        _reject_folder_name(part)
    root = workspace.resolve()
    current = root
    for part in path.parts[:-1]:
        current = current / part
        _reject_link(root, current, missing_is_error=True)
    target = current / path.parts[-1]
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _FileError("That folder is outside the workspace.", "outside_workspace") from exc
    _reject_existing_folder(target)
    return target


def _reject_folder_name(part: str) -> None:
    if part in {".", ".."} or part.endswith((" ", ".")) or any(ord(char) < 32 for char in part):
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    if any(char in part for char in '<>:"|?*\\/'):
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise _FileError("That folder is outside the workspace.", "outside_workspace")


def _reject_existing_folder(target: Path) -> None:
    if _is_reparse(target):
        raise _FileError("That folder is outside the workspace.", "outside_workspace")
    if not target.exists():
        return
    if target.is_dir():
        raise _FileError("That folder already exists.", "already_exists")
    if target.is_file():
        raise _FileError("That path is already a file.", "already_file")
    raise _FileError("That folder already exists.", "already_exists")
