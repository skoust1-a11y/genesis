"""Strategy onboarding must reject lossy execution instead of inventing rules."""

from types import SimpleNamespace

import pytest

from tests.candle_integrity_fixtures import declaration, selection_view, synthetic_calendar

pytestmark = pytest.mark.usefixtures("synthetic_calendar")

from app.agents.review_crew.graph import _audit_node, _scenario_node, _verdict_node
from app.strategies.normalizer import normalize_markdown
from app.strategies.schema import parse_strategy_markdown
from app.trading import walk_forward as wf
from tests.conftest import APPROVABLE_MARKDOWN
from tests.test_causal_walk_forward import _make_test_strategy_spec, _wf_cfg


def test_missing_timeframe_is_not_daily():
    spec = normalize_markdown(APPROVABLE_MARKDOWN.replace("timeframe: 1d\n", ""))
    assert spec.quarantined
    assert not spec.simulatable
    assert any(reason.startswith("MISSING_TIMEFRAME") for reason in spec.quarantine_reasons)


def test_unverified_prose_cannot_be_dropped_in_favor_of_machine_rules():
    source = APPROVABLE_MARKDOWN.replace("rules_authority: structured\n", "")
    source = source.replace("IV rank at least 40 with 7 to 21 days to expiry", "Buy an EMA 9/21 crossover intraday")
    spec = normalize_markdown(source)
    assert spec.quarantined and not spec.simulatable and not spec.paper_eligible
    assert any(reason.startswith("UNVERIFIED_PROSE_RULE_COVERAGE") for reason in spec.quarantine_reasons)


@pytest.mark.parametrize("field,value", [
    ("invented_indicator", 15),
    ("intraday_entry", [{"kind": "pdh_pdl_sweep"}]),
    ("session_window", {"square_off": "15:15"}),
])
def test_legacy_extensions_cannot_be_stripped(field, value):
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    doc.frontmatter["timeframe"] = "1d"
    doc.frontmatter["rules"][field] = value
    spec = normalize_markdown(doc.markdown)
    assert spec.quarantined
    assert any(f"rules.{field}" in reason for reason in spec.quarantine_reasons)


def test_intraday_hybrid_cannot_route_through_eod():
    intraday = _make_test_strategy_spec()
    doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    rules = intraday.rules_v2.model_dump(mode="json")
    rules["entry"] = [{"field": "dte", "op": ">=", "value": 1}]
    rules["exit"] = [{"kind": "profit_target_pct_of_debit", "value": 50},
                     {"kind": "stop_loss_pct_of_debit", "value": 50}]
    doc.frontmatter = {"name": "hybrid", "rules_v2": rules}
    assert doc.rules is None
    state = {"strategy": doc, **_audit_node({"strategy": doc})}
    assert state["audit"]["execution_status"] == "NOT_EXECUTABLE_EXACTLY"
    assert not state["audit"]["simulatable"]
    assert _scenario_node(state)["scenarios"] == []
    verdict = _verdict_node({**state, "scenario_results": []})["verdict"]
    assert verdict["label"] == "Not executable"
    assert verdict["rejection_reasons"]


def test_all_grid_candidates_failing_cannot_select_first():
    from tests.test_causal_walk_forward import _make_session_candles
    class EmptyGenerator:
        def generate_signals(self, *args, **kwargs):
            assert kwargs["allowed_dates"] == ("2026-11-06",)
            return []

    fold = SimpleNamespace(is_dates=("2026-11-06",), is_end_date="2026-11-06")
    selector = wf.GridSearchParameterSelector([{"ema_period": 9}, {"ema_period": 21}])
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="NO_VALID_PARAMETER_CANDIDATE"):
        selector.select_parameters(fold, _make_test_strategy_spec(), EmptyGenerator(), selection_view(_make_session_candles("2026-11-06"), ["2026-11-06"], fold), _wf_cfg())


@pytest.mark.parametrize("params", [
    {"fast_period": 9, "slow_period": 21},
    {"ema_period": 9, "unknown": 21},
])
def test_unsupported_ema_semantics_fail_before_reading_candles(params):
    spec = _make_test_strategy_spec()
    spec.rules_v2.intraday_entry[0].params = params
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="NOT_EXECUTABLE_EXACTLY"):
        wf.RuleBasedCausalSignalGenerator().generate_signals(
            spec, {}, [], allowed_dates=["2026-11-06"], config=_wf_cfg())


def test_variant_profit_cannot_rescue_losing_baseline():
    def result(label, net):
        return {"scenario": {"label": label},
                "backtest": {"net_pnl": net, "gross_pnl": net, "trades": 100},
                "forward": {"net_pnl": net, "gross_pnl": net, "trades": 100}}
    state = {"audit": {"simulatable": True, "issues": []}, "executed_baseline": True,
             "data_source": "nse_eod", "scenario_results": [result("base", -100), result("variant", 500)]}
    assert _verdict_node(state)["verdict"]["label"] == "Not profitable"
    state["executed_baseline"] = False
    assert _verdict_node(state)["verdict"]["label"] == "Not executable"


def test_forward_performance_does_not_select_variant():
    state = {"audit": {"simulatable": True, "issues": []}, "executed_baseline": True,
             "data_source": "nse_eod", "scenario_results": [
                 {"scenario": {"label": "base"},
                  "backtest": {"net_pnl": 200, "gross_pnl": 200, "trades": 100},
                  "forward": {"net_pnl": -1000, "gross_pnl": -1000, "trades": 100}},
                 {"scenario": {"label": "variant"},
                  "backtest": {"net_pnl": 100, "gross_pnl": 100, "trades": 100},
                  "forward": {"net_pnl": 10000, "gross_pnl": 10000, "trades": 100}}]}
    assert _verdict_node(state)["verdict"]["best_scenario"]["scenario"]["label"] == "base"


def test_global_period_cannot_replace_two_distinct_declared_emas():
    from app.strategies.rules import IntradayEntryPredicate
    spec = _make_test_strategy_spec()
    spec.rules_v2.intraday_entry = [IntradayEntryPredicate(
        kind="ema_relation", params={"relation": "above", "ema_period": period}, direction="LONG"
    ) for period in (9, 21)]
    spec.rules_v2.entry_composition = "ALL"
    with pytest.raises(wf.UnsupportedStrategyRuleError, match="ambiguous parameter binding"):
        wf.RuleBasedCausalSignalGenerator().generate_signals(
            spec, {"ema_period": 15}, [], allowed_dates=[], config=_wf_cfg())


def test_ai_extraction_cannot_receive_performance_verdict(client, monkeypatch):
    from app.extract.extractor import ExtractionResult
    monkeypatch.setattr("app.api.routes.extract_strategy", lambda *args, **kwargs:
                        ExtractionResult(APPROVABLE_MARKDOWN, "llm", "test", APPROVABLE_MARKDOWN))
    saved = client.post("/api/ingest/extract", json={"source_type": "notebooklm", "text": "An incompletely extracted trading strategy"})
    assert saved.status_code == 200, saved.text
    reviewed = client.post("/api/review/run", json={"strategy_id": saved.json()["strategy_id"]})
    assert reviewed.status_code == 200, reviewed.text
    report = reviewed.json()["report"]
    assert report["verdict"]["label"] == "Not executable"
    assert report["verdict"]["execution_status"] == "NOT_EXECUTABLE_EXACTLY"
    assert report["scenario_results"] == []
    assert any(reason["code"] == "UNVERIFIED_SOURCE_RULE_COVERAGE"
               for reason in report["audit"]["rejection_reasons"])


@pytest.mark.parametrize("clarified_extractor", ["llm", "passthrough"])
def test_clarification_preserves_unverified_llm_source(client, session, monkeypatch, clarified_extractor):
    from app.extract.extractor import ExtractionResult
    from app.ingest.store import IngestRunStore
    from app.strategies.store import StrategyStore

    original = "Original source with rules not independently verified"
    output = APPROVABLE_MARKDOWN
    monkeypatch.setattr("app.api.routes.extract_strategy", lambda *args, **kwargs:
                        ExtractionResult(output, "llm", "test", "raw extraction"))
    saved = client.post("/api/ingest/extract", json={"source_type": "notebooklm", "text": original}).json()
    assert parse_strategy_markdown(saved["markdown"]).frontmatter["source_verification_required"] is True
    monkeypatch.setattr("app.api.routes.get_settings", lambda: SimpleNamespace(llm_enabled=True))
    # Attempt to self-certify and explicitly remove the inherited marker.
    output = APPROVABLE_MARKDOWN.replace("rules_authority: structured", "rules_authority: structured\nsource_verification_required: false")
    monkeypatch.setattr("app.api.routes.extract_strategy", lambda *args, **kwargs:
                        ExtractionResult(output, clarified_extractor, "test", "raw clarification"))
    clarified = client.post("/api/ingest/clarify", json={
        "strategy_id": saved["strategy_id"], "answers": {"blackout": "none"}})
    assert clarified.status_code == 200, clarified.text
    session.expire_all()
    version = StrategyStore(session).latest_version(saved["strategy_id"])
    doc = parse_strategy_markdown(version.markdown)
    assert doc.frontmatter["source_verification_required"] is True
    run = IngestRunStore(session).get(version.ingest_run_id)
    assert run.source_text == original
    assert run.raw_output == "raw clarification"
    assert run.extractor == clarified_extractor
    spec = normalize_markdown(version.markdown)
    assert spec.quarantined and not spec.paper_eligible
    assert any(r.startswith("UNVERIFIED_SOURCE_RULE_COVERAGE") for r in spec.quarantine_reasons)
    session.rollback()
    # Reload through configuration and review consumers, including claimed markdown authority.
    from app.config import get_settings
    monkeypatch.setattr("app.api.routes.get_settings", get_settings)
    configured = client.get(f"/api/strategies/{saved['strategy_id']}/configuration",
                            params={"execution_mode": "historical_synthetic"})
    assert configured.status_code == 200, configured.text
    assert configured.json()["configuration"]["execution_status"] == "NOT_EXECUTABLE_EXACTLY"
    reviewed = client.post("/api/review/run", json={"strategy_id": saved["strategy_id"]})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["report"]["verdict"]["label"] == "Not executable"
    catalog = client.get("/api/strategies/catalog")
    assert catalog.status_code == 200, catalog.text
    item = next(row for row in catalog.json()["catalog"] if row["id"] == saved["strategy_id"])
    assert item["quarantined"] and not item["paper_eligible"] and not item["promotable"]
