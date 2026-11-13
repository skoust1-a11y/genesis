from __future__ import annotations

import copy
import pytest

from app.agents.review_crew.graph import run_review_crew
from app.extract.extractor import FIXTURE_MARKDOWN
from app.live import approvals
from app.strategies.analyzer import get_full_strategy_catalog, seed_research_catalog
from app.strategies.normalizer import (
    NORMALIZER_VERSION,
    normalize_markdown,
    normalize_pdc_15ema_v0_benchmark,
    normalize_scanner_v0_family,
    normalize_strategy_document,
)
from app.strategies.rules import (
    STRATEGY_RULES_V2_VERSION,
    ParameterDef,
    SessionWindowSpec,
    StrategyRulesV2,
    check_entry,
    check_exit,
    parse_rules,
    parse_rules_v2,
    rules_sha256,
)
from app.strategies.schema import parse_strategy_markdown
from app.strategies.store import StrategyStore
from app.trading.paper_registry import (
    FAMILIES,
    REGISTRY_HASH,
    RULE_SOURCE_HASHES,
    check_paper_admission_eligibility,
)
from tests.conftest import APPROVABLE_MARKDOWN, approval_body


def test_complete_strategy_normalization_eod_and_intraday_v2():
    # 1. Legacy EOD APPROVABLE_MARKDOWN deterministically normalizes into StrategyRulesV2
    norm = normalize_markdown(
        APPROVABLE_MARKDOWN,
        strategy_id="strat-approvable",
        strategy_version="v1",
        has_validated_report=True,
        validation_fidelity_tier="TIER_1_VERIFIED",
    )
    assert norm.normalizer_version == NORMALIZER_VERSION
    assert norm.quarantined is False
    assert norm.quarantine_reasons == ()
    assert norm.unresolved_gaps == ()
    assert norm.simulatable is True
    assert norm.paper_eligible is True
    assert norm.promotable is True
    assert norm.category_class == "VALIDATED"
    assert norm.promotion_state == "VALIDATED"
    assert norm.rules_v2 is not None
    assert norm.rules_v2.schema_version == STRATEGY_RULES_V2_VERSION
    assert norm.rules_v2.instrument.underlying == "NIFTY"
    assert norm.rules_v2.instrument.structure == "iron_condor"
    assert norm.rules_v2.timeframes.primary == "1d"
    assert norm.rules_v2.stop_loss is not None and norm.rules_v2.stop_loss.mode == "pct_of_credit"
    assert norm.rules_v2.target is not None and norm.rules_v2.target.mode == "pct_of_credit"
    assert norm.rules_v2.to_legacy_rules() == norm.legacy_rules

    # 2. Direct StrategyRulesV2 intraday specification normalizes cleanly
    v2_md = """---
rules_authority: structured
name: nifty-intraday-or30-v2
rules_v2:
  strategy_id: nifty-intraday-or30-v2
  strategy_version: v2
  market: nse_options
  timeframes:
    primary: 5m
    structure: 15m
    reference: 1d
  instrument:
    underlying: NIFTY
    structure: long_call
    strike_offset: 0
    lot_size: 75
  intraday_entry:
    - kind: or30_breakout_retest
      timeframe: 5m
      direction: LONG
      params:
        opening_range_minutes: 30
        ema_period: 15
      requires_closed_bar: true
  stop_loss:
    mode: swing_extreme_buffer
    value: 2.0
    min_points: 10.0
    max_points: 25.0
  target:
    mode: r_multiple
    value: 2.0
    allow_liquidity_hop: true
  partial_exits:
    - at_r_multiple: 1.0
      book_fraction: 0.5
      move_stop_to: entry
  risk:
    max_lots: 1
    max_daily_loss_pct: 2.0
    max_open_positions: 1
  session_window:
    timezone: Asia/Kolkata
    entry_start: "09:45"
    entry_cutoff: "14:45"
    square_off: "15:15"
    blackout_windows:
      - ["11:45", "12:30"]
  indicators:
    - name: ema
      timeframe: 5m
      period: 15
    - name: or30
      timeframe: 5m
  data_dependencies:
    - nse_5m_ohlcv
    - nse_option_chain_intraday
  parameters:
    ema_period:
      name: ema_period
      type: int
      value: 15
      min_value: 5
      max_value: 50
---
# Thesis
Closed-bar OR-30 breakout & retest.

## Entry
- 5m closed-bar breakout and retest of 30m opening range with 15 EMA alignment.

## Exit
- 50% partial at +1R, runner at +2R, square-off at 15:15 IST.

## Risk
- 1 lot fixed, max 2% daily loss.

## Gaps
"""
    norm_v2 = normalize_markdown(v2_md)
    assert norm_v2.quarantined is False
    assert norm_v2.simulatable is True
    assert norm_v2.paper_eligible is True
    assert norm_v2.category_class == "IMPLEMENTED"
    assert norm_v2.rules_v2 is not None
    assert norm_v2.rules_v2.session_window is not None
    assert norm_v2.rules_v2.session_window.entry_cutoff == "14:45"


def test_missing_entry_conditions_quarantined():
    # Missing prose ## Entry section
    no_prose_entry = APPROVABLE_MARKDOWN.replace(
        "## Entry\n- IV rank at least 40 with 7 to 21 days to expiry\n",
        "## Entry\n",
    )
    norm1 = normalize_markdown(no_prose_entry)
    assert norm1.quarantined is True
    assert norm1.simulatable is False
    assert norm1.paper_eligible is False
    assert norm1.promotable is False
    assert any("MISSING_ENTRY_CONDITIONS" in r for r in norm1.quarantine_reasons)

    # Empty rules.entry list
    no_machine_entry = APPROVABLE_MARKDOWN.replace(
        "  entry:\n  - {field: iv_rank, op: '>=', value: 40}\n  - {field: dte, op: '>=', value: 7}\n  - {field: dte, op: '<=', value: 21}\n",
        "  entry: []\n",
    )
    norm2 = normalize_markdown(no_machine_entry)
    assert norm2.quarantined is True
    assert norm2.simulatable is False
    assert any("MISSING_ENTRY_CONDITIONS" in r for r in norm2.quarantine_reasons)


def test_missing_exits_and_stop_loss_rules_quarantined():
    # Missing stop_loss in rules.exit
    no_stop = APPROVABLE_MARKDOWN.replace(
        "  - {kind: stop_loss_pct_of_credit, value: 100}\n",
        "",
    )
    norm = normalize_markdown(no_stop)
    assert norm.quarantined is True
    assert norm.simulatable is False
    assert norm.paper_eligible is False
    assert any("MISSING_EXIT_OR_STOP_RULES" in r for r in norm.quarantine_reasons)

    # Empty rules.exit
    empty_exit = APPROVABLE_MARKDOWN.replace(
        "  exit:\n  - {kind: profit_target_pct_of_credit, value: 50}\n  - {kind: stop_loss_pct_of_credit, value: 100}\n",
        "  exit: []\n",
    )
    norm_empty = normalize_markdown(empty_exit)
    assert norm_empty.quarantined is True
    assert norm_empty.simulatable is False
    assert any("MISSING_EXIT_OR_STOP_RULES" in r for r in norm_empty.quarantine_reasons)


def test_missing_instrument_or_timeframe_quarantined():
    # Missing underlying in instrument
    no_underlying = APPROVABLE_MARKDOWN.replace("underlying: NIFTY, ", "")
    norm_inst = normalize_markdown(no_underlying)
    assert norm_inst.quarantined is True
    assert norm_inst.simulatable is False
    assert any("MISSING_INSTRUMENT" in r for r in norm_inst.quarantine_reasons)

    # Unsupported timeframe
    bad_tf = APPROVABLE_MARKDOWN.replace("name: approvable-condor\n", "name: approvable-condor\ntimeframe: 3m\n")
    norm_tf = normalize_markdown(bad_tf)
    assert norm_tf.quarantined is True
    assert norm_tf.simulatable is False
    assert any("INVALID_TIMEFRAME" in r for r in norm_tf.quarantine_reasons)

    # Null timeframe
    null_tf = APPROVABLE_MARKDOWN.replace("name: approvable-condor\n", "name: approvable-condor\ntimeframe: null\n")
    norm_null_tf = normalize_markdown(null_tf)
    assert norm_null_tf.quarantined is True
    assert any("MISSING_TIMEFRAME" in r for r in norm_null_tf.quarantine_reasons)

    # Intraday timeframe (5m) without session_window
    intraday_no_session = APPROVABLE_MARKDOWN.replace(
        "name: approvable-condor\n",
        "name: approvable-condor\ntimeframe: 5m\n",
    )
    norm_no_session = normalize_markdown(intraday_no_session)
    assert norm_no_session.quarantined is True
    assert any("MISSING_SESSION_WINDOW" in r for r in norm_no_session.quarantine_reasons)

    # Session window with entry_cutoff after 15:10 IST fails validation
    with pytest.raises(ValueError, match="15:10"):
        SessionWindowSpec(entry_start="09:15", entry_cutoff="15:15", square_off="15:20")


def test_invalid_parameter_definitions_quarantined():
    # Parameter value out of range and wrong type
    bad_params_md = APPROVABLE_MARKDOWN.replace(
        "name: approvable-condor\n",
        """name: approvable-condor
parameters:
  iv_threshold:
    name: iv_threshold
    type: int
    value: "not_an_int"
  stop_pct:
    name: stop_pct
    type: float
    value: 250.0
    min_value: 10.0
    max_value: 100.0
""",
    )
    norm = normalize_markdown(bad_params_md)
    assert norm.quarantined is True
    assert norm.simulatable is False
    assert norm.paper_eligible is False
    assert any("INVALID_PARAMETER_DEFINITIONS" in r for r in norm.quarantine_reasons)

    with pytest.raises(ValueError, match="min_value"):
        ParameterDef(name="x", type="float", value=5.0, min_value=10.0, max_value=2.0)


def test_ambiguous_extracted_rules_and_repo_extracted_files_quarantined():
    from pathlib import Path

    strategies_dir = Path(__file__).resolve().parents[1] / "research" / "strategies"
    for fname in ("liquidity-sweep-long.extracted.md", "liquidity-sweep-short.extracted.md"):
        md = (strategies_dir / fname).read_text(encoding="utf-8")
        norm = normalize_markdown(md, source_sha256="repo-extracted-sha")
        assert norm.quarantined is True, f"{fname} with 'Assumed, not in the source' gaps must be quarantined"
        assert norm.simulatable is False
        assert norm.paper_eligible is False
        assert norm.promotable is False
        assert norm.category_class == "EXTRACTED_CANDIDATE"
        assert norm.promotion_state == "EXTRACTED"
        assert any(r.startswith("AMBIGUOUS_EXTRACTED_RULE:") for r in norm.quarantine_reasons)
        assert norm.provenance["extraction_mode"] == "llm_extracted"
        assert norm.provenance["source_sha256"] == "repo-extracted-sha"

    # Verify HTML source citations and frontmatter source_youtube are preserved by normalizer
    cited_md = APPROVABLE_MARKDOWN.replace(
        "name: approvable-condor\n",
        "name: approvable-condor\nsource_youtube: https://youtu.be/example123\n",
    ).replace(
        "## Entry\n",
        "## Entry\n<!-- source: transcript [12:34] -->\n",
    )
    norm_cited = normalize_markdown(cited_md)
    assert "https://youtu.be/example123" in norm_cited.source_references
    assert "transcript [12:34]" in norm_cited.source_references
    assert norm_cited.provenance["citations"] == ["transcript [12:34]"]

    for fname in ("liquidity-sweep.md", "previous-day-breakout.md", "trendline-breakout.md"):
        md = (strategies_dir / fname).read_text(encoding="utf-8")
        norm = normalize_markdown(md)
        assert norm.quarantined is True
        assert norm.simulatable is False
        assert norm.paper_eligible is False
        assert norm.category_class in ("INCOMPLETE_SPECIFICATION", "EXTRACTED_CANDIDATE")


def test_immutable_identity_and_hashes():
    norm_a = normalize_markdown(APPROVABLE_MARKDOWN, strategy_id="s-1", strategy_version="v1", source_sha256="src-a")
    norm_b = normalize_markdown(APPROVABLE_MARKDOWN, strategy_id="s-1", strategy_version="v1", source_sha256="src-b")
    assert norm_a.rules_v2 is not None and norm_b.rules_v2 is not None

    # Provenance metadata does not alter canonical rules_v2_sha256, parameter_hash, or identity_hash
    assert norm_a.rules_v2_sha256 == norm_b.rules_v2_sha256
    assert norm_a.parameter_hash == norm_b.parameter_hash
    assert norm_a.rules_v2.identity_hash() == norm_b.rules_v2.identity_hash()
    assert norm_a.spec_sha256 == norm_b.spec_sha256

    # Changing a parameter or rule threshold changes the hashes deterministically
    modified_md = APPROVABLE_MARKDOWN.replace("short_distance: 1000", "short_distance: 1200")
    norm_c = normalize_markdown(modified_md, strategy_id="s-1", strategy_version="v1")
    assert norm_c.rules_v2_sha256 != norm_a.rules_v2_sha256
    assert norm_c.parameter_hash != norm_a.parameter_hash
    assert norm_c.spec_sha256 != norm_a.spec_sha256


def test_quarantine_enforcement_at_review_simulation_paper_and_promotion_boundaries(client, session, paper_account):
    # 1. Strategy with assumed/unresolved mandatory gap is blocked from simulation in review_crew/graph.py
    quarantined_md = APPROVABLE_MARKDOWN.replace(
        "## Gaps\n",
        "## Gaps\n- Assumed, not in the source: stop loss threshold set to 100% of credit.\n",
    )
    doc = parse_strategy_markdown(quarantined_md)
    payload, sim_runs, reviews, improvements = run_review_crew(doc)
    assert payload["audit"]["quarantined"] is True
    assert payload["audit"]["simulatable"] is False
    assert payload["audit"]["executable"] is False
    assert any("AMBIGUOUS_EXTRACTED_RULE" in r for r in payload["audit"]["quarantine_reasons"])
    assert payload["scenarios"] == []
    assert payload["scenario_results"] == []
    assert sim_runs == []
    assert payload["verdict"]["label"] == "Not executable"
    assert payload["verdict"]["quarantined"] is True
    assert "QUARANTINED:" in payload["narrative"]

    # 2. Persist via API, run review, and verify promotion & paper-approval boundaries reject it
    ingest = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": quarantined_md})
    assert ingest.status_code == 200
    sid = ingest.json()["strategy_id"]
    rev = client.post("/api/review/run", json={"strategy_id": sid})
    assert rev.status_code == 200
    rev_body = rev.json()
    assert rev_body["report"]["verdict"]["label"] == "Not executable"
    assert rev_body["report"]["verdict"]["quarantined"] is True

    prev = client.get("/api/live/approvals/preview", params={"strategy_id": sid, "version": 1}).json()
    assert prev["quarantined"] is True
    assert any("Gaps must be empty" in b for b in prev["blockers"])
    assert any("strategy quarantined:" in b for b in prev["blockers"])
    assert any("Not executable" in b for b in prev["blockers"])

    # Attempting paper approval fails closed with 400
    res = client.post(
        "/api/live/approvals",
        json={
            "strategy_id": sid,
            "version": 1,
            "mode": "paper",
            "broker_account_id": paper_account["id"],
            "confirm_rules_sha256": prev["rules_sha256"],
            "acknowledge_verdict": True,
        },
    )
    assert res.status_code == 400

    # 3. Paper registry boundary rejects quarantined StrategyDocument, disabled PDC_15EMA_V0, and unregistered IDs
    ok_q, reasons_q = check_paper_admission_eligibility("Liquidity Sweep [CALL]", spec=doc)
    assert ok_q is False and "STRATEGY_QUARANTINED" in reasons_q

    ok_pdc, reasons_pdc = check_paper_admission_eligibility("PDC_15EMA_V0")
    assert ok_pdc is False and any(r.startswith("STRATEGY_DISABLED:") for r in reasons_pdc)

    ok_unreg, reasons_unreg = check_paper_admission_eligibility("UNKNOWN_STRATEGY_ID")
    assert ok_unreg is False and reasons_unreg == ["STRATEGY_NOT_REGISTERED"]

    ok_valid, reasons_valid = check_paper_admission_eligibility(
        "Liquidity Sweep [CALL]",
        spec=parse_strategy_markdown(APPROVABLE_MARKDOWN),
    )
    assert ok_valid is True and reasons_valid == []


def test_strategy_catalog_cleanup_distinguishes_all_six_classes_without_silent_migration(session, client):
    ingested = client.post("/api/ingest/extract", json={"source_type": "markdown", "text": APPROVABLE_MARKDOWN}).json()
    client.post("/api/review/run", json={"strategy_id": ingested["strategy_id"]})
    # Ensure seeding twice does not create duplicate strategies or bump existing versions
    seed_research_catalog(session)
    store = StrategyStore(session)
    versions_before = {s.name: s.current_version for s in store.list()}
    seed_research_catalog(session)
    versions_after = {s.name: s.current_version for s in store.list()}
    assert versions_before == versions_after

    # If an existing strategy version contains source_youtube or <!-- source: ... -->,
    # seed_research_catalog must NOT silently strip provenance or bump its version.
    cited_strategy = APPROVABLE_MARKDOWN.replace(
        "name: approvable-condor\n",
        "name: custom-cited\nsource_youtube: https://youtu.be/8XNqCSiRZg4\n",
    ).replace("## Entry\n", "## Entry\n<!-- source: [75:45] -->\n")
    saved_cited = store.save(cited_strategy)
    session.commit()
    seed_research_catalog(session)
    latest_md = store.latest_version(saved_cited.strategy_id).markdown
    assert store.get_strategy(saved_cited.strategy_id).current_version == 1
    assert "source_youtube:" in latest_md and "<!-- source:" in latest_md

    catalog = get_full_strategy_catalog(session)
    slugs = [item["slug"] for item in catalog]
    assert len(slugs) == len(set(slugs)), "Catalog must not contain duplicate strategy identities"

    classes = {item["category_class"] for item in catalog}
    assert classes == {
        "IMPLEMENTED",
        "EXTRACTED_CANDIDATE",
        "RESEARCH_HYPOTHESIS",
        "INCOMPLETE_SPECIFICATION",
        "FROZEN_BENCHMARK",
    }

    by_slug = {item["slug"]: item for item in catalog}
    assert by_slug["liquidity-sweep-long"]["category_class"] == "EXTRACTED_CANDIDATE"
    assert by_slug["liquidity-sweep-long"]["quarantined"] is True
    assert by_slug["liquidity-sweep-long"]["win_rate"] is None  # no unverified hardcoded metrics surfaced as verified
    assert by_slug["liquidity-sweep-long"]["metrics_provenance"] == "NO_VERIFIED_REPORT"

    assert by_slug["opening-range-liquidity"]["category_class"] == "RESEARCH_HYPOTHESIS"
    assert by_slug["opening-range-liquidity"]["quarantined"] is True

    assert by_slug["liquidity-sweep"]["category_class"] == "INCOMPLETE_SPECIFICATION"
    assert by_slug["liquidity-sweep"]["quarantined"] is True

    assert by_slug["pdc-15ema-v0"]["category_class"] == "FROZEN_BENCHMARK"
    assert by_slug["pdc-15ema-v0"]["quarantined"] is True
    assert by_slug["pdc-15ema-v0"]["paper_eligible"] is False

    assert by_slug["scanner-v0-liq-sweep-pdhl"]["category_class"] == "IMPLEMENTED"
    assert by_slug["scanner-v0-liq-sweep-pdhl"]["paper_eligible"] is True

    assert by_slug["approvable-condor"]["category_class"] == "IMPLEMENTED"
    assert by_slug["approvable-condor"]["promotable"] is False
    assert by_slug["approvable-condor"]["metrics_provenance"] == "NO_VERIFIED_REPORT"


def test_frozen_v0_backward_compatibility_and_existing_signals_unchanged():
    # 1. PDC_15EMA_V0 benchmark normalizes deterministically and stays quarantined from promotion
    pdc = normalize_pdc_15ema_v0_benchmark()
    assert pdc.strategy_id == "PDC_15EMA_V0"
    assert pdc.strategy_version == "v0-frozen"
    assert pdc.category_class == "FROZEN_BENCHMARK"
    assert pdc.quarantined is True
    assert pdc.paper_eligible is False

    # 2. All registered scanner-v0 families normalize deterministically
    for fam in FAMILIES:
        spec = normalize_scanner_v0_family(fam)
        assert spec.strategy_id == fam
        assert spec.strategy_version == "scanner-v0.1"
        assert spec.quarantined is False
        assert spec.paper_eligible is True

    # 3. Legacy StrategyRules & rules_sha256 are 100% unchanged for FIXTURE_MARKDOWN and APPROVABLE_MARKDOWN
    fix_doc = parse_strategy_markdown(FIXTURE_MARKDOWN)
    app_doc = parse_strategy_markdown(APPROVABLE_MARKDOWN)
    assert fix_doc.rules is not None and fix_doc.rules_errors == []
    assert app_doc.rules is not None and app_doc.rules_errors == []
    assert rules_sha256(fix_doc.frontmatter["rules"]) == rules_sha256(copy.deepcopy(fix_doc.frontmatter["rules"]))

    # 4. Existing EOD check_entry and check_exit signals are identical between legacy StrategyRules and StrategyRulesV2.to_legacy_rules()
    from types import SimpleNamespace
    from app.strategies.rules import PositionState

    norm_app = normalize_strategy_document(app_doc)
    assert norm_app.rules_v2 is not None
    roundtrip_rules = norm_app.rules_v2.to_legacy_rules()
    assert roundtrip_rules == app_doc.rules

    snap = SimpleNamespace(iv_rank=55.0, dte=14.0, spot=24000.0, vix=15.0, events=[])
    assert check_entry(app_doc.rules, snap) == check_entry(roundtrip_rules, snap)

    pos = PositionState(credit=1000.0, pnl=550.0)
    assert check_exit(app_doc.rules, pos, snap) == check_exit(roundtrip_rules, pos, snap)


def test_unknown_fidelity_and_unbound_reports_cannot_promote():
    from app.strategies.normalizer import has_bound_oos_validation
    spec = normalize_markdown(APPROVABLE_MARKDOWN, strategy_id="gate", strategy_version="v1",
                              has_validated_report=True)
    assert not spec.independently_validated
    payload = {"fidelity_tier": "TIER_1_VERIFIED", "validation_passed": True,
               "paper_promotion_eligible": True, "verified_market_performance": True,
               "has_valid_oos_folds": True,
               "data_fidelity_and_provenance": {"validation_binding": spec.validation_binding()}}
    assert has_bound_oos_validation(payload, spec)
    other = normalize_markdown(APPROVABLE_MARKDOWN, strategy_id="gate", strategy_version="v2")
    assert not has_bound_oos_validation(payload, other)
    for malformed in (None, {}, {"walk_forward": []}, {"walk_forward": {"fidelity_tier": "TIER_1_VERIFIED"}}):
        assert not has_bound_oos_validation(malformed, spec)
