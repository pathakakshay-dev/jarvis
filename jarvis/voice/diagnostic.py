"""One local diagnostic recording. It is kept only after consent and then deleted."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from jarvis.voice.transcribe import MAX_AUDIO_BYTES, audio_suffix

CONSENT_HEADER = "keep-local"
DIAGNOSTIC_TIMEOUT_S = 300
DIAGNOSTIC_NOTICE = (
    "Compared locally with Whisper small. The recording was not sent to an external service and has been deleted."
)
STORED_NOTICE = "Kept on this computer for a diagnostic comparison. The recording was not sent to an external service."


class DiagnosticStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def discard(self) -> bool:
        with self._lock:
            return self._remove_files()

    def status(self) -> dict:
        with self._lock:
            path, _content_type = self._clip()
            stored = path is not None
            return {
                "stored": stored,
                "bytes": path.stat().st_size if stored else 0,
                "audio_leaves_machine": False,
            }

    def save(self, audio: bytes, content_type: str) -> dict:
        if not audio:
            raise ValueError("empty")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("long")
        suffix = audio_suffix(content_type)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._remove_files()
            path = self.directory / f"clip{suffix}"
            path.write_bytes(audio)
            (self.directory / "clip.json").write_text(
                json.dumps({"content_type": content_type or "audio/webm", "bytes": len(audio)}),
                encoding="utf-8",
            )
        return {"stored": True, "bytes": len(audio), "audio_leaves_machine": False, "notice": STORED_NOTICE}

    def read(self) -> tuple[bytes, str]:
        with self._lock:
            path, content_type = self._clip()
            if path is None or content_type is None:
                raise FileNotFoundError("diagnostic")
            return path.read_bytes(), content_type

    def delete(self) -> bool:
        with self._lock:
            return self._remove_files()

    def _clip(self) -> tuple[Path | None, str | None]:
        meta_path = self.directory / "clip.json"
        if not meta_path.is_file():
            return None, None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        content_type = str(meta.get("content_type") or "audio/webm")
        path = self.directory / f"clip{audio_suffix(content_type)}"
        if not path.is_file():
            return None, None
        return path, content_type

    def _remove_files(self) -> bool:
        removed = False
        if not self.directory.exists():
            return False
        for name in ("clip.json", "clip.webm", "clip.wav", "clip.ogg"):
            path = self.directory / name
            if path.is_file():
                path.unlink()
                removed = True
        return removed
