from dataclasses import FrozenInstanceError

import pytest

from tests.candle_integrity_fixtures import declaration, selection_view, synthetic_calendar

pytestmark = pytest.mark.usefixtures("synthetic_calendar")
import yaml

from app.strategies.configuration import (
    create_configuration,
    create_variant,
    run_configuration,
)
from tests.test_causal_walk_forward import (
    _make_session_candles,
    _make_test_strategy_spec,
    _trading_calendar,
    _wf_cfg,
)


def configuration(strategy_id="independent", execution_mode="historical_synthetic"):
    rules = _make_test_strategy_spec(strategy_id).rules_v2.model_dump(mode="json")
    markdown = "---\n" + yaml.safe_dump({"name": strategy_id, "rules_v2": rules}) + "---\n"
    return create_configuration(
        markdown, original_source=markdown, source_type="markdown", source_ref=None,
        strategy_id=strategy_id, strategy_version="v1", execution_mode=execution_mode,
    )


def test_configuration_keeps_source_and_independent_immutable_rules():
    first, second = configuration("first"), configuration("second")
    assert first.sha256 != second.sha256
    view = first.to_dict()
    assert view["source"]["text"] == view["submitted_markdown"]
    assert view["execution_status"] == "SUPPORTED_STRUCTURED_RULES"
    view["normalized_specification"]["rules_v2"]["parameters"]["ema_period"]["value"] = 999
    assert first.to_dict()["normalized_specification"]["rules_v2"]["parameters"]["ema_period"]["value"] == 15
    with pytest.raises(FrozenInstanceError):
        first.canonical = "changed"


def test_variant_binds_parent_without_mutating_baseline():
    baseline = configuration()
    original = baseline.canonical
    variant = create_variant(baseline, strategy_version="v2", parameters={"ema_period": 9, "target_r_multiple": 3.0})
    assert baseline.canonical == original
    assert variant.sha256 != baseline.sha256
    payload = variant.to_dict()
    assert payload["parent_configuration_sha256"] == baseline.sha256
    rules = payload["normalized_specification"]["rules_v2"]
    assert rules["intraday_entry"][0]["params"]["ema_period"] == 9
    assert rules["target"]["value"] == 3.0
    assert not payload["paper_approved"] and not payload["live_enabled"]


@pytest.mark.parametrize("parameters", [{"undeclared": 1}, {"ema_period": 1000}])
def test_invalid_variant_fails_closed(parameters):
    with pytest.raises(ValueError):
        create_variant(configuration(), strategy_version="v2", parameters=parameters)


def test_replay_cannot_change_synthetic_fidelity():
    with pytest.raises(ValueError, match="EXECUTION_MODE_FIDELITY_MISMATCH"):
        run_configuration(configuration(), candles=[], trading_dates=[], config=_wf_cfg(fidelity_tier="TIER_1_VERIFIED"))


def test_configuration_replay_retains_no_oos_verdict_on_short_history():
    result = run_configuration(
        configuration(), candles=_make_session_candles("2026-11-06"),
        trading_dates=["2026-11-06"], config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"),
     dataset_declaration=declaration(["2026-11-06"]),)
    assert not result["report"]["has_valid_oos_folds"]
    assert not result["paper_approved"] and not result["live_enabled"]


def test_development_validation_cannot_grant_promotion(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("app.trading.walk_forward.run_causal_walk_forward_validation", lambda *args, **kwargs:
                        SimpleNamespace(to_dict=lambda: {
                            "report_hash": "engine-hash", "paper_promotion_eligible": True,
                            "data_fidelity_and_provenance": {"paper_promotion_eligible": True},
                            "failed_validation_criteria": [], "strategy_state": "INDEPENDENTLY_VALIDATED",
                        }))
    result = run_configuration(configuration(), candles=[], trading_dates=[],
                               config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"))
    evidence = result["report"]
    assert evidence["validation_scope"] == "DEVELOPMENT_ONLY"
    assert evidence["engine_report_hash"] == "engine-hash"
    assert not evidence["paper_promotion_eligible"]
    assert not evidence["data_fidelity_and_provenance"]["paper_promotion_eligible"]
    assert "FINAL_HOLDOUT_NOT_INDEPENDENTLY_EVALUATED" in evidence["failed_validation_criteria"]


def test_configuration_runs_reproducible_causal_walk_forward():
    dates = _trading_calendar()
    candles = [bar for day in dates for bar in _make_session_candles(day)]
    snapshot = configuration()
    cfg = _wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
    first = run_configuration(snapshot, candles=candles, trading_dates=dates, config=cfg, dataset_declaration=declaration(dates),)
    second = run_configuration(snapshot, candles=candles, trading_dates=dates, config=cfg, dataset_declaration=declaration(dates),)
    assert first["report"]["has_valid_oos_folds"]
    assert first["report"]["report_hash"] == second["report"]["report_hash"]
    assert first["report"]["strategy_id"] == "independent"
    assert not first["report"]["paper_promotion_eligible"]


def test_configuration_api_retains_original_source_and_requires_explicit_mode(client):
    baseline = configuration()
    source = baseline.to_dict()["submitted_markdown"]
    saved = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": source}).json()
    endpoint = f"/api/strategies/{saved['strategy_id']}/configuration"
    assert client.get(endpoint).status_code == 422
    assert client.get(endpoint, params={"execution_mode": "live"}).status_code == 400
    response = client.get(endpoint, params={"execution_mode": "historical_synthetic", "version": 1})
    assert response.status_code == 200, response.text
    assert response.json()["configuration"]["source"]["text"] == source
    assert response.json()["configuration_sha256"] == client.get(
        endpoint, params={"execution_mode": "historical_synthetic", "version": 1}).json()["configuration_sha256"]


def test_configuration_store_is_immutable_and_detects_corruption(tmp_path):
    from app.strategies.configuration_store import ConfigurationStore
    store = ConfigurationStore(tmp_path)
    baseline = configuration()
    assert store.save(baseline) == store.save(baseline)
    first = create_variant(baseline, strategy_version="v2", parameters={"ema_period": 9})
    second = create_variant(baseline, strategy_version="v2", parameters={"ema_period": 21})
    store.save(first)
    with pytest.raises(ValueError, match="IMMUTABLE_CONFIGURATION_VERSION_CONFLICT"):
        store.save(second)
    assert store.load(baseline.sha256).canonical == baseline.canonical
    (tmp_path / f"{baseline.sha256}.json").write_text("corrupt")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        store.load(baseline.sha256)
    with pytest.raises(ValueError, match="Invalid configuration hash"):
        store.load("../../private")


def test_persisted_configuration_and_variant_api(client, monkeypatch, tmp_path):
    from app.config import get_settings
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    source = configuration().to_dict()["submitted_markdown"]
    saved = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": source}).json()
    created = client.post(f"/api/strategies/{saved['strategy_id']}/configurations", json={
        "version": 1, "execution_mode": "historical_synthetic"})
    assert created.status_code == 200, created.text
    digest = created.json()["configuration_sha256"]
    variant = client.post(f"/api/strategy-configurations/{digest}/variants", json={
        "strategy_version": "v2", "parameters": {"ema_period": 9}})
    assert variant.status_code == 200, variant.text
    assert variant.json()["configuration"]["parent_configuration_sha256"] == digest
    assert client.get(f"/api/strategy-configurations/{digest}").json() == created.json()


@pytest.mark.parametrize("selector", [lambda *args: {"ema_period": 9}, False])
def test_configuration_rejects_unbound_selector_before_execution(selector, monkeypatch, tmp_path):
    from app.strategies.configuration_store import ConfigurationStore

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected selector reached execution or artifact persistence")

    monkeypatch.setattr("app.trading.walk_forward.run_causal_walk_forward_validation", forbidden)
    monkeypatch.setattr(ConfigurationStore, "save_result", forbidden)
    with pytest.raises(ValueError, match="UNBOUND_PARAMETER_SELECTOR"):
        run_configuration(configuration(), candles=[], trading_dates=[],
                          config=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC"),
                          parameter_selector=selector)
    assert list(tmp_path.iterdir()) == []
