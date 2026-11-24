"""In-process provider for tests and for running with no API key."""

from __future__ import annotations

import threading
import time

from jarvis.providers.base import ChatResult
from jarvis.safety.halt import HaltController


class FakeProvider:
    id = "fake"

    def __init__(self, reply: str = "This is a local fake reply.") -> None:
        self.model = "fake"
        self.reply = reply
        self.mode = "reply"
        self.error_type = "provider_error"
        self.calls = 0
        self.messages_seen: list[list[dict]] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(
        self,
        *,
        messages: list[dict],
        tool_specs: list[dict],
        halt: HaltController,
        generation: int,
        timeout_s: float,
    ) -> ChatResult:
        self.calls += 1
        self.messages_seen.append(messages)
        self.entered.set()
        if not halt.allow_outbound() or not halt.is_current(generation):
            return ChatResult(ok=False, outcome="cancelled", error_type="cancelled")
        if self.mode == "block":
            deadline = time.monotonic() + timeout_s
            while not self.release.is_set():
                if not halt.is_current(generation):
                    return ChatResult(ok=False, outcome="cancelled", error_type="cancelled")
                if time.monotonic() >= deadline:
                    return ChatResult(
                        ok=False,
                        error_type="timeout",
                        text="The request timed out.",
                    )
                time.sleep(0.01)
            if not halt.is_current(generation):
                return ChatResult(
                    ok=False,
                    outcome="already_completed",
                    error_type="already_completed",
                    text=self.reply,
                )
        if self.mode == "error":
            return ChatResult(
                ok=False,
                error_type=self.error_type,
                text="The provider returned an error.",
            )
        if self.mode == "tools":
            return ChatResult(ok=True, tool_calls=[{"id": "t1", "name": "shell", "arguments": {}}])
        if not halt.is_current(generation):
            return ChatResult(
                ok=False,
                outcome="already_completed",
                error_type="already_completed",
                text=self.reply,
            )
        return ChatResult(ok=True, text=self.reply)
