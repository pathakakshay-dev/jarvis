"""Provider contract. The agent loop depends on this shape, not on a vendor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from jarvis.safety.halt import HaltController


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    error_type: str = ""
    outcome: str = ""
    tool_calls: list = field(default_factory=list)


class ChatProvider(Protocol):
    id: str
    model: str

    def complete(
        self,
        *,
        messages: list[dict],
        tool_specs: list[dict],
        halt: HaltController,
        generation: int,
        timeout_s: float,
    ) -> ChatResult:
        """Return a result. Do not decide whether Emergency Stop is allowed."""
