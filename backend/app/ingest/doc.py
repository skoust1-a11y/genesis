from __future__ import annotations

import httpx
from bs4 import BeautifulSoup


def fetch_document_text(url: str) -> dict:
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type.lower():
            raise ValueError("PDF URLs are not parsed in v1. Paste text or upload .md.")
        html = resp.text
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    if len(text) < 200:
        raise ValueError("Document text too short or blocked. Paste content manually.")
    return {"source_type": "document", "source_url": url, "text": text[:120_000]}
