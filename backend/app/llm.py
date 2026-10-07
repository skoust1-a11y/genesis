"""The chat model for every LLM call, from LLM_PROVIDER (openai | anthropic)."""

from __future__ import annotations

from typing import Any

from app.config import Settings


def chat_model(
    settings: Settings, model: str | None = None, timeout: float | None = None, max_retries: int | None = None
) -> Any:
    kwargs: dict[str, Any] = {"model": model or settings.llm_model, "api_key": settings.llm_api_key, "temperature": 0}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**kwargs)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(**kwargs)
