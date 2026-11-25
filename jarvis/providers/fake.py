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
        self.tool_specs_seen: list[list[dict]] = []
        self.planner_reply: str | None = None
        self.goal_reply: str | None = None
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
        self.tool_specs_seen.append(tool_specs)
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
        goal_task = _goal_task(messages)
        if goal_task is not None:
            if self.goal_reply is not None:
                return ChatResult(ok=True, text=self.goal_reply)
            return ChatResult(ok=False, error_type="rejected", text="That goal is not supported.")
        desktop_task = _desktop_task(messages)
        if desktop_task is not None:
            if self.planner_reply is not None:
                return ChatResult(ok=True, text=self.planner_reply)
            from jarvis.desktop.plan import local_desktop_proposal

            return ChatResult(ok=True, text=local_desktop_proposal(desktop_task))
        task = _planner_task(messages)
        if task is not None:
            if self.planner_reply is not None:
                return ChatResult(ok=True, text=self.planner_reply)
            from jarvis.skills.orchestrator import PlanError
            from jarvis.skills.planner import proposal_from_task

            try:
                return ChatResult(ok=True, text=proposal_from_task(task))
            except PlanError as exc:
                return ChatResult(ok=False, error_type="rejected", text=exc.text)
        return ChatResult(ok=True, text=self.reply)


def _goal_task(messages: list[dict]) -> str | None:
    if not messages or not isinstance(messages[0], dict):
        return None
    system = messages[0].get("content")
    if not isinstance(system, str) or not system.startswith("JARVIS goal agent."):
        return None
    last = messages[-1].get("content") if isinstance(messages[-1], dict) else ""
    return last if isinstance(last, str) else ""


def _desktop_task(messages: list[dict]) -> str | None:
    if not messages or not isinstance(messages[0], dict):
        return None
    system = messages[0].get("content")
    if not isinstance(system, str) or not system.startswith("JARVIS desktop planner."):
        return None
    last = messages[-1].get("content") if isinstance(messages[-1], dict) else ""
    return last if isinstance(last, str) else ""


def _planner_task(messages: list[dict]) -> str | None:
    if not messages or not isinstance(messages[0], dict):
        return None
    system = messages[0].get("content")
    if not isinstance(system, str) or not system.startswith("JARVIS planner."):
        return None
    last = messages[-1].get("content") if isinstance(messages[-1], dict) else ""
    return last if isinstance(last, str) else ""
