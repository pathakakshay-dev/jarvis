"""Groq chat adapter. The key is read from settings and is not logged."""

from __future__ import annotations

import re

from jarvis.providers.base import ChatResult
from jarvis.safety.halt import HaltController

_MODEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")
_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider:
    id = "groq"

    def __init__(self, api_key: str, model: str, transport=None) -> None:
        self.api_key = api_key
        self.model = model
        self.transport = transport

    def complete(
        self,
        *,
        messages: list[dict],
        tool_specs: list[dict],
        halt: HaltController,
        generation: int,
        timeout_s: float,
    ) -> ChatResult:
        del tool_specs
        if not halt.is_current(generation) or not halt.allow_outbound():
            return ChatResult(ok=False, outcome="cancelled", error_type="cancelled")
        if not self.api_key:
            return ChatResult(
                ok=False,
                error_type="missing_key",
                text="That failed: the Groq API key is not set.",
            )
        if not _MODEL.match(self.model) or ".." in self.model:
            return ChatResult(ok=False, error_type="bad_model", text="That failed: the model id is not valid.")
        result = halt.guarded_post(
            _URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json_body={"model": self.model, "messages": messages, "temperature": 0.4},
            timeout=timeout_s,
            transport=self.transport,
        )
        return _from_openai(result, generation, halt)


def _from_openai(result, generation: int, halt: HaltController) -> ChatResult:
    if result.outcome == "cancelled":
        return ChatResult(ok=False, outcome="cancelled", error_type="cancelled")
    if result.outcome == "timeout":
        return ChatResult(ok=False, error_type="timeout", text="The request timed out.")
    if result.outcome == "http_error":
        return ChatResult(ok=False, error_type="http_error", text="That failed: the provider could not be reached.")
    response = result.response
    if response is None or 300 <= response.status_code < 400:
        return ChatResult(ok=False, error_type="http_error", text="That failed: the provider could not be reached.")
    if result.outcome == "already_completed":
        return ChatResult(ok=False, outcome="already_completed", error_type="already_completed", text=_safe_text(response))
    if response.status_code >= 400:
        return ChatResult(
            ok=False,
            error_type="http_status",
            text="That failed: the provider rejected the request.",
        )
    if not halt.is_current(generation):
        return ChatResult(
            ok=False,
            outcome="already_completed",
            error_type="already_completed",
            text=_safe_text(response),
        )
    text = _safe_text(response)
    if not text:
        return ChatResult(ok=False, error_type="bad_response", text="That failed: the provider returned no answer.")
    return ChatResult(ok=True, text=text)


def _safe_text(response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""
