"""Selects the active provider. Adding one later is a register call."""

from __future__ import annotations

from jarvis.providers.base import ChatProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ChatProvider] = {}
        self.active_id = ""

    def register(self, provider: ChatProvider) -> None:
        self._providers[provider.id] = provider
        if not self.active_id:
            self.active_id = provider.id

    def select(self, provider_id: str, model: str | None = None) -> ChatProvider:
        if provider_id not in self._providers:
            raise KeyError(provider_id)
        self.active_id = provider_id
        provider = self._providers[provider_id]
        if model:
            provider.model = model
        return provider

    def active(self) -> ChatProvider:
        return self._providers[self.active_id]

    def ids(self) -> list[str]:
        return list(self._providers)
