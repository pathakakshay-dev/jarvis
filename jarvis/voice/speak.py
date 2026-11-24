"""Speak one reply. Edge TTS sends text to Microsoft. Windows speech stays local."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jarvis.safety.halt import HaltController
from jarvis.voice.errors import VoiceCancelled
from jarvis.voice.gate import VoiceGate

MAX_SPEAK_CHARS = 1200
EDGE_VOICE_EN = "en-US-AriaNeural"
EDGE_VOICE_HI = "hi-IN-SwaraNeural"
EDGE_NOTICE = "Edge TTS sent this reply text to Microsoft. Microphone audio was not sent."
SAPI_NOTICE = "Edge TTS was unavailable. The browser will speak with Windows voices. The text stays on this computer."
SHORT_NOTICE = " Only the first part of a long reply is spoken."


@dataclass
class Speech:
    engine: str
    text: str
    audio: bytes | None
    media_type: str
    notice: str


def voice_for(text: str) -> str:
    if any("\u0900" <= char <= "\u097f" for char in text):
        return EDGE_VOICE_HI
    return EDGE_VOICE_EN


def prepare_speech(text: str) -> tuple[str, bool]:
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_SPEAK_CHARS:
        return cleaned, False
    return cleaned[:MAX_SPEAK_CHARS].rstrip(), True


class EdgeSpeaker:
    def __init__(self, timeout_s: float = 25) -> None:
        self.timeout_s = timeout_s

    async def speak(self, text: str, halt: HaltController, gate: VoiceGate, generation: int) -> Speech:
        spoken, shortened = prepare_speech(text)
        if not spoken:
            return Speech(engine="none", text="", audio=None, media_type="", notice="")
        if not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        notice = EDGE_NOTICE + (SHORT_NOTICE if shortened else "")
        fallback = SAPI_NOTICE + (SHORT_NOTICE if shortened else "")
        try:
            audio = await asyncio.wait_for(
                self._edge_audio(spoken, voice_for(spoken), halt, gate, generation),
                self.timeout_s,
            )
        except VoiceCancelled:
            raise
        except Exception:
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            return Speech(engine="sapi", text=spoken, audio=None, media_type="", notice=fallback)
        if not audio or not gate.current(generation, halt.halted):
            raise VoiceCancelled("cancelled")
        return Speech(engine="edge", text=spoken, audio=audio, media_type="audio/mpeg", notice=notice)

    async def _edge_audio(
        self,
        text: str,
        voice: str,
        halt: HaltController,
        gate: VoiceGate,
        generation: int,
    ) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []
        async for message in communicate.stream():
            if not gate.current(generation, halt.halted):
                raise VoiceCancelled("cancelled")
            if message.get("type") == "audio" and message.get("data"):
                chunks.append(message["data"])
        return b"".join(chunks)
