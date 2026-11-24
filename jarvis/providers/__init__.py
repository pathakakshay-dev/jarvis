"""Built-in providers. No vendor SDK is imported."""

from jarvis.providers.fake import FakeProvider
from jarvis.providers.gemini import GeminiProvider
from jarvis.providers.groq import GroqProvider
from jarvis.providers.registry import ProviderRegistry


def build_registry(settings, *, fake: FakeProvider | None = None) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(fake or FakeProvider())
    registry.register(GroqProvider(settings.groq_api_key, settings.groq_model))
    registry.register(GeminiProvider(settings.gemini_api_key, settings.gemini_model))
    if settings.provider in registry.ids():
        registry.select(settings.provider)
    else:
        registry.select("fake")
    return registry
