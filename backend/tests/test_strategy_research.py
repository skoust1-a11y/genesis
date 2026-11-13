import copy

import pytest

from tests.candle_integrity_fixtures import declaration, selection_view, synthetic_calendar

pytestmark = pytest.mark.usefixtures("synthetic_calendar")

from app.strategies.configuration import create_variant
from app.strategies.research import run_development_research
from tests.test_causal_walk_forward import _make_session_candles, _wf_cfg
from tests.test_strategy_configuration import configuration


def test_research_never_passes_holdout_to_candidate_runs(monkeypatch):
    baseline = configuration()
    variant = create_variant(baseline, strategy_version="v2", parameters={"ema_period": 9})
    seen = []

    def run(candidate, *, candles, trading_dates, **kwargs):
        assert trading_dates == ("2026-11-05",)
        assert len(candles) == 75
        assert all(str(row["timestamp"]).startswith("2026-11-05") for row in candles)
        seen.append(candidate.sha256)
        return {"configuration_sha256": candidate.sha256}

    monkeypatch.setattr("app.strategies.research.run_configuration", run)
    candles = _make_session_candles("2026-11-05") + _make_session_candles("2026-11-06")
    args = {"dataset_declaration": declaration(["2026-11-05", "2026-11-06"], scope="full"), "trading_dates": ["2026-11-05", "2026-11-06"], "holdout_start": "2026-11-06", "config": _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")}
    first = run_development_research((baseline, variant), candles=candles, **args)
    corrupted = copy.deepcopy(candles)
    for row in corrupted[75:]:
        row["high"] += 1  # Valid holdout change alters coordinator evidence only.
    second = run_development_research((baseline, variant), candles=corrupted, **args)
    assert first["results"] == second["results"]
    assert first["development_integrity"] == second["development_integrity"]
    assert first["coordinator_full_integrity"] != second["coordinator_full_integrity"]
    assert first["plan"] == second["plan"]
    assert seen == [baseline.sha256, variant.sha256] * 2
    invalid = copy.deepcopy(candles)
    invalid[-1]["close"] = -999999
    from app.trading.candle_integrity import CandleIntegrityError
    with pytest.raises(CandleIntegrityError):
        run_development_research((baseline, variant), candles=invalid, **args)
    assert seen == [baseline.sha256, variant.sha256] * 2
    assert first["status"] == "RESEARCH_ONLY_HOLDOUT_RESERVED"
    assert not first["final_holdout_evaluated"] and not first["paper_approved"]


def test_cross_strategy_variants_are_rejected():
    with pytest.raises(ValueError, match="same independent strategy"):
        run_development_research(
            (configuration("one"), configuration("two")), candles=[],
            trading_dates=["2026-11-05", "2026-11-06"], holdout_start="2026-11-06", config=_wf_cfg())


def test_research_endpoint_reserves_holdout_and_never_approves_paper(client, monkeypatch, tmp_path):
    from app.config import get_settings
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    baseline = configuration()
    source = baseline.to_dict()["submitted_markdown"]
    saved = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": source}).json()
    response = client.post(f"/api/strategies/{saved['strategy_id']}/research", json={
        "execution_mode": "historical_synthetic", "version": 1,
        "holdout_start": "2026-11-06", "trading_dates": ["2026-11-05", "2026-11-06"],
        "candles": _make_session_candles("2026-11-05") + _make_session_candles("2026-11-06"),
        "dataset_declaration": declaration(["2026-11-05", "2026-11-06"], scope="full"),
        "walk_forward": {"fidelity_tier": "TIER_2_SYNTHETIC"},
        "variant_parameters": [{"ema_period": 9}],
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["results"]) == 2
    assert payload["plan"]["development_dates"] == ["2026-11-05"]
    assert not payload["final_holdout_evaluated"] and not payload["paper_approved"]


@pytest.mark.parametrize("days,boundary", [
    (["2026-11-06", "2026-11-05"], "2026-11-06"),
    (["2026-11-05", "2026-11-05"], "2026-11-06"),
    (["2026-11-05"], "2026-11-06"),
])
def test_ambiguous_or_empty_research_partitions_fail_closed(days, boundary):
    with pytest.raises(ValueError):
        run_development_research((configuration(),), candles=[], trading_dates=days,
                                 holdout_start=boundary, config=_wf_cfg())


def _research_request(client):
    source = configuration().to_dict()["submitted_markdown"]
    saved = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": source}).json()
    days = ["2026-11-05", "2026-11-06"]
    return f"/api/strategies/{saved['strategy_id']}/research", {
        "execution_mode": "historical_synthetic", "version": 1, "holdout_start": days[1],
        "trading_dates": days, "candles": [r for d in days for r in _make_session_candles(d)],
        "dataset_declaration": declaration(days, scope="full"),
        "walk_forward": {"fidelity_tier": "TIER_2_SYNTHETIC"},
    }


def test_A02_real_empty_registry_rejects_before_persistence(client, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.trading import candle_calendar as cc
    endpoint, body = _research_request(client)
    monkeypatch.setenv("DATA_DIR", str(tmp_path)); get_settings.cache_clear()
    monkeypatch.setattr(cc, "_load_registry", lambda: __import__("json").loads(cc.REGISTRY_PATH.read_text()))
    response = client.post(endpoint, json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CI_CALENDAR_UNTRUSTED"
    assert not (tmp_path / "strategy-configurations").exists()


@pytest.mark.parametrize("kind", ["extra", "scope", "inline", "nested", "version", "json", "walk_forward_extra", "walk_forward_nested"])
def test_A01_A04_A06_schema_errors_sanitized(client, kind):
    endpoint, body = _research_request(client)
    sentinel = "SECRET-CANDLE-VALUE-773452"
    if kind == "extra": body[sentinel] = sentinel
    if kind == "scope": body["dataset_declaration"]["scope"] = sentinel
    if kind == "inline": body["dataset_declaration"]["calendar"] = {sentinel: sentinel}
    if kind == "nested": body["candles"] = [sentinel]
    if kind == "version": body["version"] = sentinel
    if kind == "walk_forward_extra": body["walk_forward"][sentinel] = sentinel
    if kind == "walk_forward_nested": body["walk_forward"]["is_trading_days"] = {sentinel: sentinel}
    response = (client.post(endpoint, content='{bad "'+sentinel, headers={"Content-Type": "application/json"})
                if kind == "json" else client.post(endpoint, json=body))
    assert response.status_code == 422
    assert sentinel not in response.text
    assert "input" not in response.text and "ctx" not in response.text


def test_A05_integrity_errors_sanitized_and_no_files(client, monkeypatch, tmp_path):
    from app.config import get_settings
    endpoint, body = _research_request(client)
    monkeypatch.setenv("DATA_DIR", str(tmp_path)); get_settings.cache_clear()
    body["candles"][-1]["timestamp"] = "SECRET-CANDLE-TIMESTAMP-773452"
    response = client.post(endpoint, json=body)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "CI_TIMESTAMP_INVALID"
    assert "SECRET" not in response.text
    assert set(detail) == {"code", "message", "context"}
    assert not (tmp_path / "strategy-configurations").exists()


def test_research_v2_schema_and_canonical_development_hash(monkeypatch):
    from app.strategies.research import RESEARCH_ARTIFACT_SCHEMA
    monkeypatch.setattr("app.strategies.research.run_configuration",lambda *a,**kw:{"development_integrity":kw["candles"].receipt})
    days=["2026-11-05","2026-11-06"]
    rows=[r for d in days for r in _make_session_candles(d)]
    result=run_development_research((configuration(),),candles=rows,trading_dates=days,
        holdout_start=days[-1],config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"),
        dataset_declaration=declaration(days,scope="full"))
    assert result["research_version"]==RESEARCH_ARTIFACT_SCHEMA["properties"]["research_version"]["const"]
    assert set(result)==set(RESEARCH_ARTIFACT_SCHEMA["required"])
    assert result["plan"]["development_data_sha256"]==result["development_integrity"]["candles_sha256"]
    assert result["results"][0]["development_integrity"]==result["development_integrity"]
