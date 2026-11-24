"""Gemini chat adapter. The key is sent as a header so it does not appear in the URL."""

from __future__ import annotations

import re

from jarvis.providers.base import ChatResult
from jarvis.providers.groq import _from_openai
from jarvis.safety.halt import HaltController

_MODEL = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class GeminiProvider:
    id = "gemini"

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
                text="That failed: the Gemini API key is not set.",
            )
        if not _MODEL.match(self.model):
            return ChatResult(ok=False, error_type="bad_model", text="That failed: the model id is not valid.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        result = halt.guarded_post(
            url,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json_body={"contents": _contents(messages)},
            timeout=timeout_s,
            transport=self.transport,
        )
        if result.outcome in {"cancelled", "timeout", "http_error"} or result.response is None:
            return _from_openai(result, generation, halt)
        if result.outcome == "already_completed":
            return ChatResult(
                ok=False,
                outcome="already_completed",
                error_type="already_completed",
                text=_gemini_text(result.response),
            )
        if result.response.status_code >= 400:
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
                text=_gemini_text(result.response),
            )
        text = _gemini_text(result.response)
        if not text:
            return ChatResult(ok=False, error_type="bad_response", text="That failed: the provider returned no answer.")
        return ChatResult(ok=True, text=text)


def _contents(messages: list[dict]) -> list[dict]:
    contents = []
    for message in messages:
        role = "model" if message.get("role") == "assistant" else "user"
        text = message.get("content") or ""
        if message.get("role") == "system":
            text = "Instructions: " + text
        contents.append({"role": role, "parts": [{"text": text}]})
    return contents


def _gemini_text(response) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "".join(chunks)
