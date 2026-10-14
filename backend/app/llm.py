"""The chat model for every LLM call: LLM_PROVIDER, falling back in order to FALLBACK_LLM_PROVIDER
(comma-separated; openai | anthropic | groq | google | huggingface) when one has no key or a call fails."""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

# Groq, Gemini and Hugging Face serve the OpenAI chat API, so ChatOpenAI reaches them without another dependency.
OPENAI_COMPATIBLE = {
    "groq": "https://api.groq.com/openai/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "huggingface": "https://router.huggingface.co/v1",
}

_LANGFUSE_CLIENT = None


def init_langfuse(settings: Settings) -> Any | None:
    """Initialize the Langfuse client singleton if configured."""
    global _LANGFUSE_CLIENT
    if not settings.langfuse_enabled:
        return None
    if _LANGFUSE_CLIENT is not None:
        return _LANGFUSE_CLIENT
    try:
        from langfuse import Langfuse

        _LANGFUSE_CLIENT = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_base_url,
        )
        return _LANGFUSE_CLIENT
    except Exception as exc:
        logger.warning("Failed to initialize Langfuse client: %s", exc)
        return None


def langfuse_callback(settings: Settings) -> Any | None:
    """Instantiate a per-call LangChain callback handler for Langfuse tracing."""
    if not settings.langfuse_enabled:
        return None
    try:
        init_langfuse(settings)
        from langfuse.langchain import CallbackHandler

        return CallbackHandler(public_key=settings.langfuse_public_key)
    except Exception as exc:
        logger.warning("Failed to create Langfuse callback handler: %s", exc)
        return None


def flush_langfuse() -> None:
    """Flush any queued Langfuse traces."""
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        pass


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
    settings: Settings,
    model: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    callbacks: list[Any] | None = None,
) -> Any:
    """`model` overrides LLM_PROVIDER's model only; a fallback always uses its own configured model."""
    extra = {k: v for k, v in (("timeout", timeout), ("max_retries", max_retries)) if v is not None}
    cb = list(callbacks or [])
    lf_cb = langfuse_callback(settings)
    if lf_cb is not None and not any(isinstance(c, type(lf_cb)) for c in cb):
        cb.append(lf_cb)
    if cb:
        extra["callbacks"] = cb

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
