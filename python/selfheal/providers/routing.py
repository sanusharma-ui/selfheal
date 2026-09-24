from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from .base import AIProvider
from .compatible import OpenAICompatibleProvider
from .errors import ProviderUnavailable


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, *, api_key: str, model: str, **options):
        options.setdefault("base_url", "https://api.openai.com/v1")
        super().__init__(api_key=api_key, model=model, **options)


class GroqProvider(OpenAICompatibleProvider):
    def __init__(self, *, api_key: str, model: str, **options):
        options.setdefault("base_url", "https://api.groq.com/openai/v1")
        super().__init__(api_key=api_key, model=model, **options)


class FallbackProvider(AIProvider):
    """Explicit cross-provider consent; only availability errors trigger fallback."""
    def __init__(self, providers: Sequence[AIProvider]):
        if not providers:
            raise ValueError("At least one provider is required")
        self.providers = tuple(providers)

    def propose_repair(self, request):
        for provider in self.providers:
            try:
                return provider.propose_repair(request)
            except ProviderUnavailable:
                continue
        raise ProviderUnavailable("All configured providers are unavailable")


def provider_from_env(
    *, priority: Sequence[str] | None = None, fallback: bool = False,
    environ: Mapping[str, str] | None = None, **options,
) -> AIProvider:
    """Select available credentials; never silently mix a vendor's key/endpoint."""
    env = os.environ if environ is None else environ
    if priority is None:
        priority = tuple(env.get("SELFHEAL_PROVIDER_PRIORITY", "groq,openai").split(","))
    names = tuple(name.strip().lower() for name in priority)
    if not names or len(set(names)) != len(names) or any(n not in ("groq", "openai") for n in names):
        raise ValueError("priority must contain unique provider names: groq, openai")
    providers = []
    for name in names:
        prefix = name.upper()
        key = env.get(prefix + "_API_KEY", "").strip()
        if not key:
            continue
        model = env.get(prefix + "_MODEL", "").strip()
        if not model:
            raise ValueError(f"Set {prefix}_MODEL for the configured {name} provider")
        cls = GroqProvider if name == "groq" else OpenAIProvider
        providers.append(cls(api_key=key, model=model, **options))
        if not fallback:
            return providers[0]
    if not providers:
        raise ValueError("Set GROQ_API_KEY and GROQ_MODEL, or OPENAI_API_KEY and OPENAI_MODEL")
    return FallbackProvider(providers)
