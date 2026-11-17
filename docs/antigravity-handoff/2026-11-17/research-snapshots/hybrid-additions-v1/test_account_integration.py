"""Hybrid policy and multi-fill research journal integration, entirely isolated."""
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from app.trading import forward_zone_candidate as zone
from app.trading.forward_fees import policy
from app.trading.paper_exit_policy import FRESH_BID
from tests.test_forward_comparison import NOW, config, quote, raw, signal, tick


def make_account(config,tmp_path):
    path=Path(__file__).with_name('research_account.py')
    spec=importlib.util.spec_from_file_location('offline_hybrid_integration',path)
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
    engine=SimpleNamespace(broader=False,advance=lambda rows,state,now:zone.advance(rows,state))
    a=m.HybridAccount(tmp_path/'account.db',config.model_copy(update={'paper_exit_quote_policy':FRESH_BID}),
        run_id='offline-hybrid-test',signal_engine=engine,legacy_management=True,
        dynamic_selection=False,hopping=True,levels_source=lambda now:[25070,25100])
    a.five_source=lambda now:[]
    return a


def observe(a,monkeypatch,now,spot,bid,signals=None,low=None):
    monkeypatch.setattr(zone,'advance',lambda rows,state:(state,signals or []))
    chain,evidence=quote(now,price=bid)
    frame=raw(now,spot)
    if low is not None:frame.loc[frame.index[-1],'low']=low
    a.tick(now=now,raw_1m=frame,chain=chain,evidence=evidence,news_safe=True,
        fee_policy=policy(now.date().isoformat()).policy_id)


def get(a):
    with a.connect() as c:
        row=dict(c.execute('SELECT * FROM trades').fetchone())
        return row,json.loads(row['body']),[json.loads(f[0]) for f in c.execute('SELECT body FROM fills ORDER BY id')]


def test_breakeven_update_is_persisted_and_not_retroactive(config,tmp_path,monkeypatch):
    a=make_account(config,tmp_path)
    try:
        tick(a,config,monkeypatch)
        observe(a,monkeypatch,NOW+timedelta(minutes=1),25008,21,low=24999)
        row,t,fills=get(a)
        assert row['state']=='OPEN' and t['runner_stop']==25000 and len(fills)==1
        observe(a,monkeypatch,NOW+timedelta(minutes=2),24999,19)
        row,t,fills=get(a)
        assert row['state']=='CLOSED' and row['exit_reason']=='TRAILING_STOP_HIT'
        assert len(fills)==2 and fills[-1]['qty']==195
    finally:a.close()


def test_new_opposite_confirmation_is_available_before_exit_management(config,tmp_path,monkeypatch):
    a=make_account(config,tmp_path)
    try:
        tick(a,config,monkeypatch)
        now=NOW+timedelta(minutes=1)
        opposite={**signal(now,id='opposite'),'side':'PUT','stop_spot':25020}
        observe(a,monkeypatch,now,25001,20,signals=[opposite])
        row,t,fills=get(a)
        assert row['state']=='CLOSED' and row['exit_reason']=='REVERSAL_EXIT'
        assert len(fills)==2  # opposite lacks a verified PE quote: no automatic new order
    finally:a.close()


def test_two_r_milestone_then_hop_preserves_whole_lots_and_monotonic_stop(config,tmp_path,monkeypatch):
    a=make_account(config,tmp_path)
    try:
        tick(a,config,monkeypatch)
        observe(a,monkeypatch,NOW+timedelta(minutes=1),25008,21)
        observe(a,monkeypatch,NOW+timedelta(minutes=2),25025,28)
        assert get(a)[1]['runner_stop']==25008
        observe(a,monkeypatch,NOW+timedelta(minutes=3),25031,30)
        row,t,fills=get(a)
        assert row['state']=='OPEN' and t['remaining_lots']==2 and t['runner_stop']==25027
        observe(a,monkeypatch,NOW+timedelta(minutes=4),25040,35)
        assert get(a)[1]['runner_stop']==25027
        observe(a,monkeypatch,NOW+timedelta(minutes=5),25071,40)
        row,t,fills=get(a)
        assert row['state']=='CLOSED' and len(fills)==3
        assert [f['qty'] for f in fills]==[195,65,130]
    finally:a.close()


def test_stagnation_exits_at_fresh_book_without_invented_threshold_fill(config,tmp_path,monkeypatch):
    a=make_account(config,tmp_path)
    try:
        tick(a,config,monkeypatch)
        observe(a,monkeypatch,NOW+timedelta(minutes=31),25001,17)
        row,t,fills=get(a)
        assert row['state']=='CLOSED' and row['exit_reason']=='STAGNATION_TIME_STOP'
        assert fills[-1]['price']=='16.95'
    finally:a.close()
