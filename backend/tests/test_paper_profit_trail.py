import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from app.trading import forward_zone_candidate as zone
from app.trading import paper_profit_trail as trail
from app.trading.forward_candidate_account import CandidateAccount
from app.trading.forward_fees import policy as fee_policy
from app.trading.paper_exit_policy import FRESH_BID
from tests.test_forward_comparison import (
    NOW,
    config,  # noqa: F401 - imported pytest fixture
    quote,
    raw,
    signal,
    tick,
)


def test_percentage_tracks_peak_profit_not_peak_price_or_fixed_amount():
    mark = trail.evaluate(previous_peak=50, current_net=40, initial_risk=20, percent=20)
    assert mark.peak == 50 and mark.threshold == 40 and mark.triggered
    larger = trail.evaluate(previous_peak=50, current_net=100, initial_risk=20, percent=20)
    assert larger.threshold == 80 and not larger.triggered
    smaller = trail.evaluate(previous_peak=100, current_net=81, initial_risk=20, percent=20)
    assert smaller.threshold == 80 and not smaller.triggered


def test_activation_and_fractional_paisa_are_conservative():
    assert not trail.evaluate(previous_peak=0, current_net=99, initial_risk=100, percent=20).armed
    assert trail.evaluate(previous_peak=0, current_net=100, initial_risk=100, percent=20).armed
    mark = trail.evaluate(previous_peak="100.01", current_net="80.00", initial_risk=100, percent=20)
    assert mark.threshold == Decimal("80.01") and mark.triggered
    for value in (9, 21, "NaN"):
        with pytest.raises(ValueError):
            trail.profile(value)


def observed(account, monkeypatch, now, *, bid, spot=25035, stale=False):
    chain, evidence = quote(now, price=bid)
    if stale:
        chain["contracts"][0]["ce"]["liveData"]["quote_time"] = (now-timedelta(seconds=11)).isoformat()
    monkeypatch.setattr(zone, "advance", lambda rows, state: (state, []))
    return account.tick(now=now, raw_1m=raw(now, spot), chain=chain, evidence=evidence,
                        news_safe=True, fee_policy=fee_policy(now.date().isoformat()).policy_id)


def test_winner_runs_beyond_3r_and_peak_survives_restart(config, tmp_path, monkeypatch):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    path = tmp_path / "trail.db"
    account = CandidateAccount(path, settings, run_id="trail")
    tick(account, settings, monkeypatch)
    cash_after_entry = account.stats()["cash_inr"]
    observed(account, monkeypatch, NOW+timedelta(minutes=1), bid=30)
    assert account.stats()["closed"] == 0  # underlying has already exceeded the old 3R target
    observed(account, monkeypatch, NOW+timedelta(minutes=2), bid=40, spot=25050)
    assert account.stats()["cash_inr"] == cash_after_entry  # hypothetical liquidation marks are never fills
    with account.connect() as c:
        peak = Decimal(c.execute("SELECT peak FROM profit_trails").fetchone()[0])
    account.close()
    account = CandidateAccount(path, settings, run_id="trail")
    try:
        observed(account, monkeypatch, NOW+timedelta(minutes=3), bid=38, spot=25040)
        assert account.stats()["closed"] == 0
        with account.connect() as c:
            assert Decimal(c.execute("SELECT peak FROM profit_trails").fetchone()[0]) == peak
        observed(account, monkeypatch, NOW+timedelta(minutes=4), bid=35, spot=25040)
        assert account.stats()["closed"] == 1
        with account.connect() as c:
            assert c.execute("SELECT exit_reason FROM trades").fetchone()[0] == "PEAK_PROFIT_GIVEBACK"
    finally:
        account.close()


def test_stale_marks_do_not_raise_peak_or_invent_a_trailing_fill(config, tmp_path, monkeypatch):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(tmp_path / "trail.db", settings, run_id="trail")
    try:
        tick(account, settings, monkeypatch)
        observed(account, monkeypatch, NOW+timedelta(minutes=1), bid=40)
        with account.connect() as c:
            peak = c.execute("SELECT peak FROM profit_trails").fetchone()[0]
        observed(account, monkeypatch, NOW+timedelta(minutes=2), bid=100, stale=True)
        with account.connect() as c:
            assert c.execute("SELECT peak FROM profit_trails").fetchone()[0] == peak
        assert account.stats()["closed"] == 0
        observed(account, monkeypatch, NOW+timedelta(minutes=3), bid=10, spot=25035)
        assert account.stats()["closed"] == 1  # gap realizes the current book, never the old threshold
    finally:
        account.close()


@pytest.mark.parametrize("closing", ["stop", "square_off"])
def test_initial_stop_and_squareoff_remain_in_force(config, tmp_path, monkeypatch, closing):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(tmp_path / "trail.db", settings, run_id="trail")
    try:
        tick(account, settings, monkeypatch)
        now = NOW+timedelta(minutes=1) if closing == "stop" else NOW.replace(hour=15, minute=10)
        observed(account, monkeypatch, now, bid=19, spot=24985 if closing == "stop" else 25001)
        assert account.stats()["closed"] == 1
        with account.connect() as c:
            assert c.execute("SELECT exit_reason FROM trades").fetchone()[0] == ("STOP" if closing == "stop" else "SQUARE_OFF")
    finally:
        account.close()


def test_trail_parameters_require_new_journals(config, tmp_path):
    path = tmp_path / "trail.db"
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(path, settings, run_id="trail")
    account.close()
    with pytest.raises(ValueError, match="new run"):
        CandidateAccount(path, settings.model_copy(update={"paper_candidate_profit_giveback_pct": Decimal(10)}), run_id="trail")


def test_quote_only_monitor_tracks_between_candles_and_is_idempotent(config, tmp_path, monkeypatch):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(tmp_path / "trail.db", settings, run_id="trail")
    try:
        tick(account, settings, monkeypatch)

        def source(now, bid):
            chain, _ = quote(now, price=bid)
            return {"optionChain": {"contracts":chain["contracts"], "aggregatedDetails":{"currentExpiry":chain["expiry"]}}}

        first = NOW+timedelta(seconds=10)
        account.monitor_profit(now=first, chain_source=lambda:source(first,40), fee_policy=fee_policy("2026-11-16").policy_id)
        assert account.stats()["closed"] == 0
        now = NOW+timedelta(seconds=20)
        errors = []

        def close():
            try:
                account.monitor_profit(now=now, chain_source=lambda:source(now,35), fee_policy=fee_policy("2026-11-16").policy_id)
            except Exception as exc:  # noqa: BLE001 - surface any background-worker failure in the assertion
                errors.append(exc)

        workers = [threading.Thread(target=close) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        assert not errors and not any(worker.is_alive() for worker in workers)
        assert account.stats()["closed"] == 1
        with account.connect() as c:
            assert c.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0] == 1
            assert c.execute("SELECT marked_at FROM profit_trails").fetchone()[0] == now.isoformat()
    finally:
        account.close()


def test_profit_peak_cannot_come_from_a_quote_before_entry(config, tmp_path, monkeypatch):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(tmp_path / "trail.db", settings, run_id="trail")
    try:
        tick(account, settings, monkeypatch)
        now = NOW+timedelta(seconds=1)
        chain, _ = quote(now, price=100)
        chain["contracts"][0]["ce"]["liveData"]["quote_time"] = (NOW-timedelta(seconds=1)).isoformat()
        source = {"optionChain":{"contracts":chain["contracts"],"aggregatedDetails":{"currentExpiry":chain["expiry"]}}}
        account.monitor_profit(now=now, chain_source=lambda:source, fee_policy=fee_policy("2026-11-16").policy_id)
        with account.connect() as c:
            assert c.execute("SELECT count(*) FROM profit_trails").fetchone()[0] == 0
        assert account.stats()["closed"] == 0
    finally:
        account.close()


def test_monitor_failure_blocks_new_risk(config, tmp_path, monkeypatch):
    settings = config.model_copy(update={"paper_exit_quote_policy": FRESH_BID,
                                          "paper_candidate_profit_giveback_pct": Decimal(20)})
    account = CandidateAccount(tmp_path / "trail.db", settings, run_id="trail")
    try:
        chain, evidence = quote()
        monkeypatch.setattr(zone, "advance", lambda rows, state: (state, [signal()]))
        result = account.tick(now=NOW, raw_1m=raw(), chain=chain, evidence=evidence,
            news_safe=True, fee_policy=fee_policy("2026-11-16").policy_id, admission_blocker="PROFIT_MONITOR_FAILED")
        assert result["reasons"] == ["PROFIT_MONITOR_FAILED"]
        assert account.stats()["entries"] == 0
        assert account.stats()["cash_inr"] == "50000.00"
    finally:
        account.close()
