from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.review_crew.graph import run_review_crew
from app.api.common import or_400, or_404
from app.config import get_settings
from app.db.models import StrategyVersion
from app.db.serialize import row_to_dict
from app.db.session import get_db
from app.db.types import utcnow
from app.extract.extractor import ExtractionError, ExtractionResult, extract_strategy
from app.ingest.doc import fetch_document_text
from app.ingest.instagram import fetch_instagram_caption
from app.ingest.store import IngestRunStore
from app.ingest.youtube import fetch_youtube_transcript
from app.provenance import report_inputs
from app.reports.store import ReportStore
from app.strategies.clarify import answers_as_hints, clarifying_questions
from app.strategies.schema import StrategyDocument, parse_strategy_markdown
from app.strategies.store import StrategyStore

router = APIRouter(prefix="/api")

FIXTURE_NOTES = Path(__file__).resolve().parents[2] / "fixtures" / "notebooklm_notes.txt"


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


# Ingest


def _fetch_source(body: IngestRequest) -> dict:
    if body.source_type == "youtube":
        if not body.url:
            raise ValueError("url required")
        return fetch_youtube_transcript(body.url)
    if body.source_type == "instagram":
        if not body.url:
            raise ValueError("url required")
        return fetch_instagram_caption(body.url)
    if body.source_type in {"document", "notebooklm"}:
        if body.text:
            return {"source_type": body.source_type, "source_url": body.url, "text": body.text}
        if body.url:
            source = fetch_document_text(body.url)
            source["source_type"] = body.source_type
            return source
        raise ValueError("text or url required")
    raise ValueError("unsupported source_type")


def _record_failed_ingest(db: Session, source: dict, hints: str, started: datetime, exc: Exception) -> None:
    db.rollback()
    IngestRunStore(db).record(
        source=source,
        hints=hints,
        started_at=started,
        status="failed",
        extractor="llm" if isinstance(exc, ExtractionError) else None,
        model=getattr(exc, "model", None),
        raw_output=getattr(exc, "raw_output", None),
        error=f"{type(exc).__name__}: {exc}",
    )
    db.commit()


def _ingest(db: Session, source: dict, extraction: ExtractionResult, hints: str, started: datetime):
    parse_strategy_markdown(extraction.markdown)
    run = IngestRunStore(db).record(
        source=source,
        hints=hints,
        started_at=started,
        status="completed",
        extractor=extraction.extractor,
        model=extraction.model,
        raw_output=extraction.raw_output,
    )
    saved = StrategyStore(db).save(extraction.markdown, ingest_run_id=run.id)
    db.commit()
    return run, saved


@router.post("/ingest/extract")
def ingest_and_extract(body: IngestRequest, db: Session = Depends(get_db)):
    started = utcnow()
    source = {"source_type": body.source_type, "source_url": body.url, "text": body.text}
    try:
        if body.source_type == "markdown":
            if not body.text:
                raise ValueError("markdown text required")
            extraction = ExtractionResult(body.text, "passthrough", None, body.text)
        else:
            source = _fetch_source(body)
            extraction = extract_strategy(source, hints=body.hints)
        run, saved = _ingest(db, source, extraction, body.hints, started)
    except ValueError as exc:
        _record_failed_ingest(db, source, body.hints, started, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _record_failed_ingest(db, source, body.hints, started, exc)
        raise
    return _extracted(saved, run)


def _extracted(saved, run) -> dict:
    return {
        "strategy_id": saved.strategy_id,
        "name": saved.doc.name,
        "markdown": saved.doc.markdown,
        "gaps": saved.doc.gaps,
        "questions": clarifying_questions(saved.doc),
        "version": saved.version.version,
        "ingest_run_id": run.id,
    }


class ClarifyRequest(BaseModel):
    strategy_id: str
    answers: dict[str, str]


@router.post("/ingest/clarify")
def clarify_and_reextract(body: ClarifyRequest, db: Session = Depends(get_db)):
    """Re-extract the strategy from its original source with the author's answers as hints."""
    strategies = StrategyStore(db)
    version = or_404(strategies.latest_version, body.strategy_id)
    if not version.ingest_run_id:
        raise HTTPException(status_code=400, detail="This version was not ingested from a source; edit it directly")
    if not get_settings().llm_enabled:
        raise HTTPException(status_code=400, detail="Answering questions needs an LLM key; edit the markdown directly")
    prior = IngestRunStore(db).get(version.ingest_run_id)
    new_hints = answers_as_hints(clarifying_questions(parse_strategy_markdown(version.markdown)), body.answers)
    if not new_hints:
        raise HTTPException(status_code=400, detail="No answers given")
    hints = "\n\n".join(h for h in (prior.hints, new_hints) if h)
    source = {"source_type": prior.source_type, "source_url": prior.source_url, "text": prior.source_text}
    started = utcnow()
    try:
        extraction = extract_strategy(source, hints=hints)
        parse_strategy_markdown(extraction.markdown)
        run = IngestRunStore(db).record(
            source=source, hints=hints, started_at=started, status="completed",
            extractor=extraction.extractor, model=extraction.model, raw_output=extraction.raw_output,
        )  # fmt: skip
        saved = strategies.save(extraction.markdown, strategy_id=body.strategy_id, ingest_run_id=run.id)
        db.commit()
    except ValueError as exc:
        _record_failed_ingest(db, source, hints, started, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _extracted(saved, run)


@router.get("/ingest-runs/{ingest_run_id}")
def get_ingest_run(ingest_run_id: str, db: Session = Depends(get_db)):
    return row_to_dict(or_404(IngestRunStore(db).get, ingest_run_id))


# Review


def _review_and_persist(db: Session, strategy_id: str, version: StrategyVersion, doc: StrategyDocument):
    started = utcnow()
    inputs = report_inputs(get_settings(), doc, version.id, version.content_sha256)
    reports = ReportStore(db)
    try:
        payload, sim_runs, reviews, improvements = run_review_crew(doc)
    except Exception as exc:
        db.rollback()
        reports.create_failed(
            strategy_id=strategy_id,
            version=version,
            error=f"{type(exc).__name__}: {exc}",
            inputs=inputs,
            started_at=started,
        )
        db.commit()
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    report = reports.create_success(
        strategy_id=strategy_id,
        version=version,
        payload=payload,
        sim_runs=sim_runs,
        reviews=reviews,
        improvements=improvements,
        inputs=inputs,
        started_at=started,
    )
    db.commit()
    return report, payload


@router.post("/review/run")
def run_review(body: ReviewRequest, db: Session = Depends(get_db)):
    strategies = StrategyStore(db)

    def resolve():
        if body.markdown:
            saved = strategies.save(body.markdown, strategy_id=body.strategy_id)
            return saved.strategy_id, saved.version
        if body.strategy_id:
            return body.strategy_id, strategies.latest_version(body.strategy_id)
        raise ValueError("strategy_id or markdown required")

    try:
        strategy_id, version = or_400(resolve)
    except HTTPException:
        db.rollback()
        raise
    db.commit()
    doc = parse_strategy_markdown(version.markdown)
    report, payload = _review_and_persist(db, strategy_id, version, doc)
    return {
        "report_id": report.id,
        "strategy_id": strategy_id,
        "strategy_name": doc.name,
        "strategy_version": version.version,
        "report": payload,
    }


@router.post("/demo/run")
def run_demo(db: Session = Depends(get_db)):
    started = utcnow()
    source = {"source_type": "notebooklm", "source_url": "fixture:notebooklm_notes.txt"}
    try:
        source["text"] = FIXTURE_NOTES.read_text(encoding="utf-8")
        extraction = extract_strategy(source)
        _, saved = _ingest(db, source, extraction, "", started)
    except ValueError as exc:
        _record_failed_ingest(db, source, "", started, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _record_failed_ingest(db, source, "", started, exc)
        raise
    report, payload = _review_and_persist(db, saved.strategy_id, saved.version, saved.doc)
    return {
        "strategy_id": saved.strategy_id,
        "markdown": saved.doc.markdown,
        "report": payload,
        "report_id": report.id,
    }


# Strategies


def _strategy_summary(strategy) -> dict:
    return {
        "id": strategy.id,
        "name": strategy.name,
        "current_version": strategy.current_version,
        "created_at": strategy.created_at,
        "archived_at": strategy.archived_at,
    }


@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)):
    return {"strategies": [_strategy_summary(s) for s in StrategyStore(db).list()]}


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str, db: Session = Depends(get_db)):
    store = StrategyStore(db)
    strategy = or_404(store.get_strategy, strategy_id)
    version = or_404(store.latest_version, strategy_id)
    doc = parse_strategy_markdown(version.markdown)
    return {
        "strategy_id": strategy_id,
        "markdown": doc.markdown,
        "gaps": doc.gaps,
        "questions": clarifying_questions(doc),
        "version": version.version,
        "archived_at": strategy.archived_at,
    }


@router.get("/strategies/{strategy_id}/versions")
def list_versions(strategy_id: str, db: Session = Depends(get_db)):
    versions = or_404(StrategyStore(db).versions, strategy_id)
    return {"versions": [row_to_dict(v, exclude=("markdown",)) for v in versions]}


@router.get("/strategies/{strategy_id}/versions/{number}")
def get_version(strategy_id: str, number: int, db: Session = Depends(get_db)):
    return row_to_dict(or_404(StrategyStore(db).version, strategy_id, number))


@router.post("/strategies/{strategy_id}/archive")
def archive_strategy(strategy_id: str, db: Session = Depends(get_db)):
    strategy = or_404(StrategyStore(db).archive, strategy_id)
    db.commit()
    return _strategy_summary(strategy)


# Reports


@router.get("/reports")
def list_reports(
    strategy_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = ReportStore(db).list(strategy_id=strategy_id, status=status, limit=limit, offset=offset)
    return {"reports": [row_to_dict(r, exclude=("payload", "inputs")) for r in rows]}


@router.get("/reports/{report_id}")
def get_report(report_id: str, db: Session = Depends(get_db)):
    return row_to_dict(or_404(ReportStore(db).get, report_id))


@router.get("/reports/{report_id}/simulations")
def list_simulations(report_id: str, db: Session = Depends(get_db)):
    return {"simulations": [row_to_dict(s) for s in or_404(ReportStore(db).simulations, report_id)]}


def _trade_dict(trade, run) -> dict:
    item = row_to_dict(trade)
    item["scenario_index"] = run.scenario_index
    item["mode"] = run.mode
    return item


@router.get("/reports/{report_id}/trades")
def list_trades(
    report_id: str,
    scenario_index: int | None = None,
    mode: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = or_404(ReportStore(db).trades, report_id, scenario_index, mode, limit, offset)
    return {"trades": [_trade_dict(t, r) for t, r in rows]}


REVIEW_CSV_FIELDS = ("process_verdict", "mistake_tags", "lesson", "improvement_suggestion")


@router.get("/reports/{report_id}/trades.csv")
def export_trades_csv(report_id: str, db: Session = Depends(get_db)):
    store = ReportStore(db)
    rows = or_404(store.trades, report_id)
    reviews = store.latest_reviews([t.id for t, _ in rows])
    out = io.StringIO()
    writer: csv.DictWriter | None = None
    for trade, run in rows:
        item = _trade_dict(trade, run)
        review = reviews.get(trade.id)
        for f in REVIEW_CSV_FIELDS:
            item[f"review_{f}"] = getattr(review, f) if review else None
        flat = {k: json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else v for k, v in item.items()}
        if writer is None:
            writer = csv.DictWriter(out, fieldnames=list(flat.keys()))
            writer.writeheader()
        writer.writerow(flat)
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="trades-{report_id}.csv"'},
    )
