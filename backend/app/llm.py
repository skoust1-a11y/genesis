"""The chat model for every LLM call: LLM_PROVIDER, falling back in order to FALLBACK_LLM_PROVIDER
(comma-separated; openai | anthropic | groq | google | huggingface) when one has no key or a call fails."""

from __future__ import annotations

from typing import Any

from app.config import Settings

# Groq, Gemini and Hugging Face serve the OpenAI chat API, so ChatOpenAI reaches them without another dependency.
OPENAI_COMPATIBLE = {
    "groq": "https://api.groq.com/openai/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "huggingface": "https://router.huggingface.co/v1",
}


def _provider_model(settings: Settings, provider: str, model: str, extra: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {"model": model, "api_key": settings.provider_key(provider), "temperature": 0, **extra}
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**kwargs)
    from langchain_openai import ChatOpenAI

    if provider in OPENAI_COMPATIBLE:
        kwargs["base_url"] = OPENAI_COMPATIBLE[provider]
    return ChatOpenAI(**kwargs)


def chat_model(
    settings: Settings, model: str | None = None, timeout: float | None = None, max_retries: int | None = None
) -> Any:
    """`model` overrides LLM_PROVIDER's model only; a fallback always uses its own configured model."""
    extra = {k: v for k, v in (("timeout", timeout), ("max_retries", max_retries)) if v is not None}
    first, *rest = settings.llm_providers or [settings.llm_provider]
    model = model if first == settings.llm_provider else None
    llm = _provider_model(settings, first, model or settings.provider_model(first), extra)
    if not rest:
        return llm
    return llm.with_fallbacks([_provider_model(settings, p, settings.provider_model(p), extra) for p in rest])


def served_model(resp: Any, settings: Settings) -> str:
    """The model that answered `resp`; after a fallback this is not settings.llm_model."""
    meta = getattr(resp, "response_metadata", None) or {}
    return meta.get("model_name") or meta.get("model") or settings.llm_model
