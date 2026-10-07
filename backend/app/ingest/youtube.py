from __future__ import annotations

import re

from youtube_transcript_api import YouTubeTranscriptApi


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    raise ValueError("Could not parse YouTube video id from URL")


def fetch_youtube_transcript(url: str) -> dict:
    video_id = extract_video_id(url)
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)
        transcript = fetched.to_raw_data()
    except Exception as exc:
        raise ValueError(
            "No captions available for this video. Paste NotebookLM notes or upload .md instead."
        ) from exc
    lines = []
    for item in transcript:
        ts = int(item.get("start", 0))
        mm, ss = divmod(ts, 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {item.get('text', '')}")
    text = "\n".join(lines)
    return {
        "source_type": "youtube",
        "source_url": url,
        "video_id": video_id,
        "text": text,
    }
