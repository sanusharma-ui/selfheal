from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import load_dotenv

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
    """Load cwd/.env, then select credentials without mixing vendor endpoints.

    Process variables win over .env. An explicit environ is used as-is.
    Explicit priority wins over SELFHEAL_USE_GROQ; otherwise the boolean pins
    selection to Groq (true) or OpenAI (false), including when fallback=True.
    """
    if environ is None:
        load_dotenv(Path.cwd() / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    env = os.environ if environ is None else environ
    selected = None
    if priority is None:
        if "SELFHEAL_USE_GROQ" in env:
            use_groq = env["SELFHEAL_USE_GROQ"].strip().lower()
            if use_groq not in ("true", "false"):
                raise ValueError("SELFHEAL_USE_GROQ must be true (Groq) or false (OpenAI)")
            selected = "groq" if use_groq == "true" else "openai"
            priority = (selected,)
        else:
            priority = tuple(env.get("SELFHEAL_PROVIDER_PRIORITY", "groq,openai").split(","))
    names = tuple(name.strip().lower() for name in priority)
    if not names or len(set(names)) != len(names) or any(n not in ("groq", "openai") for n in names):
        raise ValueError("priority must contain unique provider names: groq, openai")
    providers = []
    for name in names:
        prefix = name.upper()
        key = env.get(prefix + "_API_KEY", "").strip()
        if not key:
            if selected:
                raise ValueError(f"Set {prefix}_API_KEY and {prefix}_MODEL for the selected {name} provider")
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
