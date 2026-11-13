import copy
from types import SimpleNamespace

import pytest

from app.trading import candle_integrity as ci
from app.trading import walk_forward as wf
from tests.candle_integrity_fixtures import declaration, snapshot, synthetic_calendar
from tests.test_causal_walk_forward import _make_session_candles, _make_test_strategy_spec, _wf_cfg, _trading_calendar

pytestmark=pytest.mark.usefixtures("synthetic_calendar")


def test_R05_R06_exact_prefix_including_warmup():
    days=_trading_calendar(100)
    rows=[r for d in days for r in _make_session_candles(d)]
    dev=snapshot(rows,days)
    folds=wf.generate_walk_forward_folds(days,_wf_cfg())
    for fold in folds:
        view=ci.derive_is_snapshot(dev,fold=fold)
        expected=days[:days.index(fold.is_end_date)+1]
        assert len(view)==75*len(expected)
        assert sorted({ci.local_day(r["timestamp"]) for r in view})==expected
        assert not hasattr(view,"receipt") and not hasattr(view,"declaration")
        assert all(x is not dev for x in vars(view).values())
        assert dev.receipt["candles_sha256"] not in str(vars(view))
        ci.require_is_view(view,fold=fold,requirements=ci.CandleRequirements("NIFTY"))
        for forged in (copy.copy(view), SimpleNamespace(scope="is_view"), [], view.receipt if hasattr(view,"receipt") else {}):
            with pytest.raises(ci.CandleIntegrityError):
                ci.require_is_view(forged,fold=fold,requirements=ci.CandleRequirements("NIFTY"))
        with pytest.raises(ci.CandleIntegrityError) as exc:
            ci.execution_snapshot(view,declaration=None,requirements=ci.CandleRequirements("NIFTY"),trading_dates=expected)
        assert exc.value.code=="CI_SNAPSHOT_SCOPE_MISMATCH"


@pytest.mark.parametrize("legacy",[False,True])
def test_R06_runner_selector_uses_authentic_prefix(legacy):
    days=_trading_calendar(100)
    rows=[r for d in days for r in _make_session_candles(d)]
    spec=_make_test_strategy_spec();cfg=_wf_cfg(fidelity_tier="TIER_2_SYNTHETIC")
    seen=[]
    class Generator(wf.CausalSignalGenerator):
        def generate_signals(self,spec,parameters,candles,**kwargs):
            # The legacy branch's selection generation is identifiable by issued scope.
            if candles.scope=="is_view":
                cutoff=kwargs["window_end_date"]
                expected=days[:days.index(cutoff)+1]
                assert sorted({ci.local_day(r["timestamp"]) for r in candles})==expected
                assert len(candles)==75*len(expected)
                assert not hasattr(candles,"receipt")
                seen.append(cutoff)
            return []
    class Selector(wf.CausalParameterSelector):
        def select_parameters(self,fold,spec,generator,candles,config,settings=None,option_quote_provider=None):
            ci.require_is_view(candles,fold=fold,requirements=ci.requirements_for(spec))
            generator.generate_signals(spec,{},candles,allowed_dates=fold.is_dates,window_end_date=fold.is_end_date)
            return {},fold.is_dates
    def callable_selector(fold,events,executions,spec):
        assert events==[] and executions==[]
        return {},fold.is_dates
    report=wf.run_causal_walk_forward_validation(spec,candles=rows,trading_dates=days,
        signal_generator=Generator(),config=cfg,parameter_selector=callable_selector if legacy else Selector(),
        dataset_declaration=declaration(days))
    assert report.has_valid_oos_folds
    assert seen==[f.fold.is_end_date for f in report.folds]


def test_C04_direct_grid_rejects_raw_and_copied_views():
    days=["2026-11-05"]
    fold=SimpleNamespace(is_dates=tuple(days),is_end_date=days[-1])
    view=ci.derive_is_snapshot(snapshot(_make_session_candles(days[0]),days),fold=fold)
    selector=wf.GridSearchParameterSelector([{}]);spec=_make_test_strategy_spec()
    for raw,code in (([],"CI_SNAPSHOT_REQUIRED"),(copy.copy(view),"CI_SNAPSHOT_INVALID")):
        with pytest.raises(ci.CandleIntegrityError) as exc:
            selector.select_parameters(fold,spec,None,raw,_wf_cfg())
        assert exc.value.code==code
