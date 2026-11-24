"""Cancels in-flight transcription and speech without replaying them."""

from __future__ import annotations

import threading


class VoiceGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.generation = 0

    def capture(self) -> int:
        with self._lock:
            return self.generation

    def cancel(self) -> None:
        with self._lock:
            self.generation += 1

    def current(self, generation: int, halted: bool) -> bool:
        with self._lock:
            return (not halted) and generation == self.generation
