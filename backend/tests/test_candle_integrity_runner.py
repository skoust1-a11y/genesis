import copy

import pytest
from app.trading import candle_integrity as ci
from app.trading import walk_forward as wf
from tests.candle_integrity_fixtures import declaration, snapshot, synthetic_calendar
from tests.test_causal_walk_forward import _make_session_candles, _make_test_strategy_spec, _wf_cfg

pytestmark=pytest.mark.usefixtures("synthetic_calendar")
DAYS=["2026-11-05"]


@pytest.mark.parametrize("events,candles,generator", [([],[],None),([],None,object()),([],[],object())])
def test_C03_mixed_modes(events,candles,generator):
    with pytest.raises(ci.CandleIntegrityError) as exc:
        wf.run_walk_forward_validation(_make_test_strategy_spec(),trading_dates=DAYS,
            events=events,candles=candles,signal_generator=generator,config=_wf_cfg())
    assert exc.value.code=="CI_INPUT_MODE_AMBIGUOUS"


@pytest.mark.parametrize("runner",[wf.run_causal_walk_forward_validation,wf.run_walk_forward_validation])
@pytest.mark.parametrize("bad,code",[("empty","CI_DATASET_EMPTY"),("missing_declaration","CI_DECLARATION_REQUIRED"),
    ("incomplete","CI_SESSION_INCOMPLETE"),("wrong_underlying","CI_UNDERLYING_MISMATCH")])
def test_D01_D02_D04_no_fold_gate(runner,bad,code):
    rows=_make_session_candles(DAYS[0]);dec=declaration(DAYS)
    if bad=="empty":rows=[]
    if bad=="missing_declaration":dec=None
    if bad=="incomplete":rows.pop()
    if bad=="wrong_underlying":rows[0]["underlying"]="BANKNIFTY"
    class Forbidden(wf.CausalSignalGenerator):
        def generate_signals(self,*args,**kwargs):pytest.fail("invalid dataset reached callback")
    with pytest.raises(ci.CandleIntegrityError) as exc:
        runner(_make_test_strategy_spec(),trading_dates=DAYS,candles=rows,signal_generator=Forbidden(),
            config=_wf_cfg(),dataset_declaration=dec)
    assert exc.value.code==code


def test_D05_fixed_events_uncertified_and_C11_report_hash_separate():
    spec=_make_test_strategy_spec();cfg=_wf_cfg()
    fixed=wf.run_walk_forward_validation(spec,trading_dates=DAYS,events=[],config=cfg)
    assert "dataset_integrity" not in fixed.to_dict()
    rows=_make_session_candles(DAYS[0]);dec=declaration(DAYS)
    a=wf.run_causal_walk_forward_validation(spec,trading_dates=DAYS,candles=rows,config=cfg,dataset_declaration=dec)
    rows[0]["high"]+=1
    b=wf.run_causal_walk_forward_validation(spec,trading_dates=DAYS,candles=rows,config=cfg,dataset_declaration=dec)
    assert a.report_hash==b.report_hash
    assert a.dataset_integrity!=b.dataset_integrity
    da,db=a.to_dict(),b.to_dict();da.pop("dataset_integrity");db.pop("dataset_integrity")
    assert da==db


def test_D06_stronger_requirements_and_wrong_scope():
    rows=_make_session_candles(DAYS[0]);dec=declaration(DAYS,volume_provenance="synthetic")
    from app.trading.candle_calendar import resolve_approved_calendar
    dev=ci.validate_candle_dataset(rows,declaration=dec,calendar=resolve_approved_calendar(dec["calendar_id"]),
        requirements=ci.CandleRequirements("NIFTY"),trading_dates=DAYS)
    with pytest.raises(ci.CandleIntegrityError) as exc:
        ci.execution_snapshot(dev,declaration=None,requirements=ci.CandleRequirements("NIFTY",True),trading_dates=DAYS)
    assert exc.value.code=="CI_VOLUME_PROVENANCE_INCOMPATIBLE"
    full=snapshot(rows,DAYS,scope="full")
    with pytest.raises(ci.CandleIntegrityError) as exc:
        ci.execution_snapshot(full,declaration=None,requirements=ci.CandleRequirements("NIFTY"),trading_dates=DAYS)
    assert exc.value.code=="CI_SNAPSHOT_SCOPE_MISMATCH"


def test_P03_baseline_signal_fill_fee_risk_and_report_hash_parity():
    """Expected values come from immutable baseline execution, not the new runner."""
    import subprocess
    import sys
    import types
    from pathlib import Path
    from tests.test_causal_walk_forward import _trading_calendar
    root=Path(__file__).resolve().parents[2]
    source=subprocess.check_output(["git","show","af8d9f79bc24f9572f1aff13b703795c89403219:backend/app/trading/walk_forward.py"],cwd=root,text=True)
    name="app.trading._phase0a_baseline"
    baseline=types.ModuleType(name);baseline.__file__=wf.__file__;sys.modules[name]=baseline
    try:
        exec(compile(source,"baseline_walk_forward.py","exec"),baseline.__dict__)
        days=_trading_calendar();rows=[r for d in days for r in _make_session_candles(d)]
        spec=_make_test_strategy_spec();cfg=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
        before=baseline.run_causal_walk_forward_validation(spec,candles=copy.deepcopy(rows),trading_dates=days,config=cfg)
        after=wf.run_causal_walk_forward_validation(spec,candles=rows,trading_dates=days,config=cfg,dataset_declaration=declaration(days))
        actual=after.to_dict();actual.pop("dataset_integrity")
        assert actual==before.to_dict()
        assert after.report_hash==before.report_hash
        assert after.folds and any(f.oos_executions for f in after.folds)
    finally:
        sys.modules.pop(name,None)


def test_V03_zero_prefix_never_calls_vwap_fallback(monkeypatch):
    spec=_make_test_strategy_spec()
    spec.rules_v2.intraday_entry[0].kind="vwap_relation"
    spec.rules_v2.intraday_entry[0].params={}
    spec.rules_v2.indicators=[]
    rows=_make_session_candles(DAYS[0])
    for row in rows: row["volume"]=0
    real_vwap=wf.regime.compute_session_vwap
    def forbidden(*args,**kwargs): pytest.fail("zero prefix reached VWAP fallback")
    monkeypatch.setattr(wf.regime,"compute_session_vwap",forbidden)
    assert wf.RuleBasedCausalSignalGenerator().generate_signals(spec,{},rows,allowed_dates=DAYS,config=_wf_cfg())==[]
    calls=[]
    # Regime classification retains its baseline price-only input; this test inspects
    # the entry predicate's actual volume-bearing prefix.
    # The weighted predicate is checked separately from baseline regime labeling.
    def weighted(frame):
        if "volume" not in frame:
            return real_vwap(frame)
        assert frame["volume"].sum()>0
        calls.append(len(frame))
        return ((frame["high"]+frame["low"]+frame["close"])/3*frame["volume"]).sum()/frame["volume"].sum()
    monkeypatch.setattr(wf.regime,"compute_session_vwap",weighted)
    rows[20]["volume"]=100
    wf.RuleBasedCausalSignalGenerator().generate_signals(spec,{},rows,allowed_dates=DAYS,config=_wf_cfg())
    assert calls and min(calls)>=21
