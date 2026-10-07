from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agents.review_crew.graph import run_review_crew
from app.config import get_settings
from app.extract.extractor import extract_strategy_markdown
from app.ingest.doc import fetch_document_text
from app.ingest.instagram import fetch_instagram_caption
from app.ingest.youtube import fetch_youtube_transcript
from app.strategies.schema import parse_strategy_markdown
from app.strategies.store import StrategyStore

router = APIRouter(prefix="/api")
store = StrategyStore()


class IngestRequest(BaseModel):
    source_type: str = Field(description="youtube | instagram | document | notebooklm | markdown")
    url: str | None = None
    text: str | None = None
    hints: str = ""


class ReviewRequest(BaseModel):
    strategy_id: str | None = None
    markdown: str | None = None


@router.get("/health")
def health():
    settings = get_settings()
    return {"status": "ok", "llm_enabled": settings.llm_enabled}


@router.post("/ingest/extract")
def ingest_and_extract(body: IngestRequest):
    try:
        if body.source_type == "markdown":
            if not body.text:
                raise ValueError("markdown text required")
            source = {"source_type": "markdown", "text": body.text}
            markdown = body.text
        else:
            if body.source_type == "youtube":
                if not body.url:
                    raise ValueError("url required")
                source = fetch_youtube_transcript(body.url)
            elif body.source_type == "instagram":
                if not body.url:
                    raise ValueError("url required")
                source = fetch_instagram_caption(body.url)
            elif body.source_type in {"document", "notebooklm"}:
                if body.text:
                    source = {
                        "source_type": body.source_type,
                        "source_url": body.url,
                        "text": body.text,
                    }
                elif body.url:
                    source = fetch_document_text(body.url)
                    source["source_type"] = body.source_type
                else:
                    raise ValueError("text or url required")
            else:
                raise ValueError("unsupported source_type")
            markdown = extract_strategy_markdown(source, hints=body.hints)
        sid, doc = store.save(markdown)
        return {
            "strategy_id": sid,
            "name": doc.name,
            "markdown": doc.markdown,
            "gaps": doc.gaps,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/strategies")
def list_strategies():
    return {"strategies": store.list_ids()}


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str):
    try:
        doc = store.get(strategy_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="strategy not found") from exc
    return {"strategy_id": strategy_id, "markdown": doc.markdown, "gaps": doc.gaps}


@router.post("/review/run")
def run_review(body: ReviewRequest):
    try:
        if body.markdown:
            sid, doc = store.save(body.markdown, strategy_id=body.strategy_id)
        elif body.strategy_id:
            sid = body.strategy_id
            doc = store.get(sid)
        else:
            raise ValueError("strategy_id or markdown required")
        report = run_review_crew(doc)
        settings = get_settings()
        reports_dir = Path(settings.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_id = str(uuid.uuid4())
        payload = {
            "report_id": report_id,
            "strategy_id": sid,
            "strategy_name": doc.name,
            "report": report,
        }
        (reports_dir / f"{report_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="strategy not found") from exc


@router.post("/demo/run")
def run_demo():
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "notebooklm_notes.txt"
    text = fixture.read_text(encoding="utf-8")
    source = {"source_type": "notebooklm", "text": text}
    markdown = extract_strategy_markdown(source)
    sid, doc = store.save(markdown)
    report = run_review_crew(doc)
    return {"strategy_id": sid, "markdown": doc.markdown, "report": report}
