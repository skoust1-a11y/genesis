from __future__ import annotations

import re

import httpx


def fetch_instagram_caption(url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text
    og_desc = _meta(html, "og:description")
    og_title = _meta(html, "og:title")
    text = "\n".join(x for x in [og_title, og_desc] if x)
    if not text.strip():
        raise ValueError(
            "Could not read Instagram caption from URL. Paste caption text or NotebookLM notes."
        )
    return {"source_type": "instagram", "source_url": url, "text": text}


def _meta(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"',
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        rf'<meta[^>]+content="([^"]+)"[^>]+property="{prop}"',
        html,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None
