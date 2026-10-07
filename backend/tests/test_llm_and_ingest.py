"""The LLM provider switch and the YouTube caption language fallback."""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from youtube_transcript_api import NoTranscriptFound

from app.config import Settings
from app.ingest import youtube
from app.llm import chat_model


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_chat_model_follows_llm_provider():
    openai = settings(OPENAI_API_KEY="sk-test", OPENAI_MODEL="gpt-test")
    assert isinstance(chat_model(openai), ChatOpenAI) and openai.llm_model == "gpt-test"

    claude = settings(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-test", ANTHROPIC_MODEL="claude-test")
    model = chat_model(claude, timeout=5, max_retries=0)
    assert isinstance(model, ChatAnthropic) and model.model == "claude-test"
    assert claude.llm_enabled and claude.opinion_model == "claude-test"

    assert not settings(LLM_PROVIDER="anthropic", OPENAI_API_KEY="sk-test").llm_enabled  # the other provider's key


class FakeTranscript:
    def __init__(self, language_code: str, lines: list[str]) -> None:
        self.language_code = language_code
        self.lines = lines

    def fetch(self):
        return self

    def to_raw_data(self) -> list[dict]:
        return [{"start": i * 61, "text": t} for i, t in enumerate(self.lines)]


class FakeList:
    def __init__(self, transcripts: list[FakeTranscript]) -> None:
        self.transcripts = transcripts

    def __iter__(self):
        return iter(self.transcripts)

    def find_transcript(self, languages):
        for code in languages:
            for t in self.transcripts:
                if t.language_code == code:
                    return t
        raise NoTranscriptFound("vid", languages, self)


def fake_api(monkeypatch, transcripts: list[FakeTranscript]) -> None:
    class Api:
        def list(self, video_id):
            return FakeList(transcripts)

    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", Api)


URL = "https://youtu.be/M8QHS7l69eQ"


@pytest.mark.parametrize(
    ("available", "language"),
    [
        ([FakeTranscript("hi", ["namaste"]), FakeTranscript("en", ["hello"])], "en"),
        ([FakeTranscript("ta", ["vanakkam"]), FakeTranscript("hi", ["namaste"])], "hi"),
        ([FakeTranscript("ta", ["vanakkam"])], "ta"),
    ],
)
def test_youtube_prefers_english_then_hindi_then_any(monkeypatch, available, language):
    fake_api(monkeypatch, available)
    out = youtube.fetch_youtube_transcript(URL)
    assert out["language"] == language and out["video_id"] == "M8QHS7l69eQ"
    assert out["text"].startswith("[00:00] ")


def test_youtube_without_captions_says_so(monkeypatch):
    fake_api(monkeypatch, [])
    with pytest.raises(ValueError, match="No captions available"):
        youtube.fetch_youtube_transcript(URL)
