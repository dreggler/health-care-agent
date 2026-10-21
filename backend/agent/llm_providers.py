from __future__ import annotations

import backend.agent.llm.anthropic_provider as _anthropic
import backend.agent.llm.bedrock as _bedrock
import backend.agent.llm.gemini as _gemini
import backend.agent.llm.openai_provider as _openai
import backend.agent.llm.zhipuai_provider as _zhipuai
from backend.config import settings

from .llm import (
    BaseLLMProvider,
    LLMProvider,
    get_env_api_key,
    get_provider_class,
)

_DEFAULT_MODELS: dict[LLMProvider, str] = {
    LLMProvider.GEMINI:         settings.DEFAULT_MODEL_GEMINI,
    LLMProvider.OPENAI:         settings.DEFAULT_MODEL_OPENAI,
    LLMProvider.AZURE_OPENAI:   settings.DEFAULT_MODEL_AZURE_OPENAI,
    LLMProvider.ANTHROPIC:      settings.DEFAULT_MODEL_ANTHROPIC,
    LLMProvider.MISTRAL:        settings.DEFAULT_MODEL_MISTRAL,
    LLMProvider.GROQ:           settings.DEFAULT_MODEL_GROQ,
    LLMProvider.DEEPSEEK:       settings.DEFAULT_MODEL_DEEPSEEK,
    LLMProvider.XAI:            settings.DEFAULT_MODEL_XAI,
    LLMProvider.OPENROUTER:     settings.DEFAULT_MODEL_OPENROUTER,
    LLMProvider.PERPLEXITY:     settings.DEFAULT_MODEL_PERPLEXITY,
    LLMProvider.GOOGLE_VERTEX:  settings.DEFAULT_MODEL_GOOGLE_VERTEX,
    LLMProvider.AMAZON_BEDROCK: settings.DEFAULT_MODEL_AMAZON_BEDROCK,
    LLMProvider.MINIMAX:        settings.DEFAULT_MODEL_MINIMAX,
    LLMProvider.ZHIPUAI:        settings.DEFAULT_MODEL_ZHIPUAI,
    LLMProvider.VLLM:           settings.VLLM_MODEL,
}


def create_provider(
    provider_name: str,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs,
) -> BaseLLMProvider:
    try:
        prov = LLMProvider(provider_name.lower())
    except ValueError:
        valid = [p.value for p in LLMProvider]
        raise ValueError(
            f"Unknown LLM provider: '{provider_name}'. Supported: {valid}"
        ) from None

    resolved_model = model or _DEFAULT_MODELS[prov]

    if api_key is None and prov != LLMProvider.VLLM:
        api_key = get_env_api_key(prov)

    provider_cls = get_provider_class(prov)
    if not provider_cls:
        raise RuntimeError(f"Provider class for '{prov}' not registered. Check imports.")

    if prov == LLMProvider.ANTHROPIC:
        thinking_budget = kwargs.pop("thinking_budget", 1024)
        return provider_cls(model=resolved_model, api_key=api_key, thinking_budget=thinking_budget, **kwargs)
    if prov == LLMProvider.GEMINI:
        thinking_budget = kwargs.pop("thinking_budget", 0)
        return provider_cls(model=resolved_model, api_key=api_key, thinking_budget=thinking_budget, **kwargs)

    if prov == LLMProvider.VLLM:
        if api_key is None:
            api_key = "token-abc"
        kwargs.setdefault("base_url", settings.VLLM_BASE_URL)
    elif prov == LLMProvider.AZURE_OPENAI:
        kwargs.setdefault("azure_endpoint", settings.AZURE_OPENAI_ENDPOINT)
        kwargs.setdefault("api_version", settings.AZURE_OPENAI_API_VERSION)
    elif prov == LLMProvider.AMAZON_BEDROCK:
        kwargs.setdefault("region_name", settings.AWS_REGION)
    elif prov == LLMProvider.GOOGLE_VERTEX:
        kwargs.setdefault("project", settings.GOOGLE_CLOUD_PROJECT)
        kwargs.setdefault("location", settings.GOOGLE_CLOUD_LOCATION)

    return provider_cls(model=resolved_model, api_key=api_key, **kwargs)


__all__ = [
    "create_provider",
    "LLMProvider",
    "BaseLLMProvider",
    "get_env_api_key",
    "get_provider_class",
]
