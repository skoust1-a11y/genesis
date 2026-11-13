from unittest.mock import MagicMock, patch

from app.config import Settings
from app.llm import chat_model, flush_langfuse, langfuse_callback


def test_langfuse_settings_disabled():
    s = Settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="dummy",
        LANGFUSE_SECRET_KEY=None,
        LANGFUSE_PUBLIC_KEY=None,
    )
    assert s.langfuse_enabled is False
    assert langfuse_callback(s) is None


def test_langfuse_settings_enabled():
    s = Settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="dummy",
        LANGFUSE_SECRET_KEY="sk-lf-test",
        LANGFUSE_PUBLIC_KEY="pk-lf-test",
        LANGFUSE_BASE_URL="https://cloud.langfuse.com",
    )
    assert s.langfuse_enabled is True
    cb = langfuse_callback(s)
    assert cb is not None


def test_flush_langfuse_handles_exceptions():
    # Should not raise even if client cannot flush
    with patch("langfuse.get_client", side_effect=RuntimeError("no client")):
        flush_langfuse()
