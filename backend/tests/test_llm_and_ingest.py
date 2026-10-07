"""The LLM provider switch, the YouTube caption language fallback and extraction of long sources."""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from youtube_transcript_api import NoTranscriptFound

from app.config import Settings, get_settings
from app.extract import extractor
from app.extract.extractor import FIXTURE_MARKDOWN, SINGLE_PASS_CHARS, extract_strategy, split_into_chunks
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


class FakeLLM:
    def __init__(self) -> None:
        self.batches: list[list] = []
        self.invoked: list[list] = []

    def batch(self, inputs):
        self.batches.append(inputs)
        return [AIMessage(content=f"- [00:0{i}] rule from part {i}") for i in range(1, len(inputs) + 1)]

    def invoke(self, messages):
        self.invoked.append(messages)
        return AIMessage(content=FIXTURE_MARKDOWN)


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    llm = FakeLLM()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
    monkeypatch.setattr(extractor, "chat_model", lambda settings: llm)
    return llm


def transcript(chars: int) -> str:
    line = "[12:34] " + "x" * 52 + "\n"
    return line * (chars // len(line) + 1)


def test_short_source_is_extracted_in_one_call(fake_llm):
    text = transcript(SINGLE_PASS_CHARS // 2)
    result = extract_strategy({"source_type": "youtube", "text": text})
    assert fake_llm.batches == [] and len(fake_llm.invoked) == 1
    assert fake_llm.invoked[0][1].content.endswith(f"TEXT:\n{text}")
    assert result.markdown == FIXTURE_MARKDOWN.strip() and result.raw_output == FIXTURE_MARKDOWN


def test_long_source_is_condensed_part_by_part_before_extraction(fake_llm):
    text = transcript(int(SINGLE_PASS_CHARS * 1.9))
    result = extract_strategy({"source_type": "youtube", "text": text})

    (parts,) = fake_llm.batches
    sent = [msgs[1].content.split("\n\n", 1)[1] for msgs in parts]
    assert len(parts) == 3 and "".join(sent) == text  # every line, in order, nothing dropped
    assert parts[0][1].content.startswith("Part 1 of 3:")

    final = fake_llm.invoked[0][1].content
    assert "NOTES (condensed part by part" in final and "TEXT:" not in final
    assert all(f"rule from part {i}" in final for i in (1, 2, 3))
    assert result.markdown == FIXTURE_MARKDOWN.strip()
    assert result.raw_output.startswith("# Condensed notes") and result.raw_output.endswith(FIXTURE_MARKDOWN)


def test_chunks_cover_the_text_and_split_overlong_lines():
    text = "a" * 25 + "\n" + "b" * 10 + "\n" + "c" * 3
    chunks = split_into_chunks(text, size=12)
    assert "".join(chunks) == text and all(len(c) <= 12 for c in chunks)
    assert split_into_chunks("") == []
