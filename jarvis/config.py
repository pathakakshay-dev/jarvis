"""Process settings. Secrets come from the environment or a local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def _positive_float(value: str, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number


def _steps(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 4
    return max(1, min(4, number))


def _voice_timeout(value: str) -> float:
    # A 20 second clip of Whisper small on this CPU finishes in about realtime when the
    # first decode is accepted, and can take several times longer if decoding is retried.
    return min(_positive_float(value, 180), 180)


def _whisper_device(value: str) -> str:
    choice = (value or "auto").strip().lower()
    if choice in {"auto", "cpu", "cuda"}:
        return choice
    return "auto"


def _port(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 8765
    if 1 <= number <= 65535:
        return number
    return 8765


@dataclass
class Settings:
    provider: str
    groq_api_key: str
    groq_model: str
    gemini_api_key: str
    gemini_model: str
    timeout_s: float
    max_steps: int
    tool_timeout_s: float
    host: str
    port: int
    data_dir: Path
    root: Path
    voice_timeout_s: float = 180
    whisper_model: str = "small"
    whisper_device: str = "auto"


def load_settings(root: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
    root = root or project_root()
    file_values = _parse_dotenv(root / ".env")
    source = os.environ if environ is None else environ
    merged = dict(file_values)
    for key, value in source.items():
        if key.startswith("JARVIS_"):
            merged[key] = value
    provider = merged.get("JARVIS_PROVIDER", "fake").strip().lower() or "fake"
    return Settings(
        provider=provider,
        groq_api_key=merged.get("JARVIS_GROQ_API_KEY", "").strip(),
        groq_model=merged.get("JARVIS_GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b",
        gemini_api_key=merged.get("JARVIS_GEMINI_API_KEY", "").strip(),
        gemini_model=merged.get("JARVIS_GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        timeout_s=_positive_float(merged.get("JARVIS_CHAT_TIMEOUT_S", "45"), 45),
        max_steps=_steps(merged.get("JARVIS_MAX_STEPS", "4")),
        tool_timeout_s=20,
        host="127.0.0.1",
        port=_port(merged.get("JARVIS_PORT", "8765")),
        data_dir=root / "data",
        root=root,
        voice_timeout_s=_voice_timeout(merged.get("JARVIS_VOICE_TIMEOUT_S", "180")),
        whisper_model="small",
        whisper_device=_whisper_device(merged.get("JARVIS_WHISPER_DEVICE", "auto")),
    )
