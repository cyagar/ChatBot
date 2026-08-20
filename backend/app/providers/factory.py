from functools import lru_cache

from app.config import get_settings
from app.providers.base import AIProvider


@lru_cache(maxsize=1)
def get_provider() -> AIProvider:
    settings = get_settings()
    if settings.ai_provider == "local_extractive":
        from app.providers.extractive import ExtractiveProvider
        return ExtractiveProvider()
    if settings.ai_provider == "anthropic":
        from app.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if settings.ai_provider == "openai":
        from app.providers.openai_provider import OpenAIProvider
        return OpenAIProvider()
    raise ValueError(f"Unknown AI_PROVIDER: {settings.ai_provider!r}")
