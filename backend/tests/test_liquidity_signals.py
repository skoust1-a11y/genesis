from datetime import date

from app import alerts
from app.config import get_settings
from app.liquidity_signals import (
    charts,
    estimated_plan,
    message,
    option_bars,
    scan,
    weekly_expiry,
)

# 15m bars (high, low, close) from 09:15: sweep of PDL 100 at 09:30, swing high 120 at 10:00, swing low 104 at
# 10:30, then a 15m close above 120 at 11:00.
DAY = [(112, 105, 106), (108, 98, 100), (115, 101, 114), (120, 110, 112),
       (116, 106, 108), (114, 104, 107), (119, 108, 118), (125, 117, 124)]  # fmt: skip


def bars(day_open=110.0):
    out = []
    for k, (h, low, c) in enumerate(DAY):
        for m in range(3):
            minutes = 9 * 60 + 15 + 15 * k + 5 * m
            out.append({"date": f"2026-10-14 {minutes // 60:02d}:{minutes % 60:02d}", "open": c,
                        "high": h, "low": low, "close": c})  # fmt: skip
    out[0]["open"] = day_open
    return out


def test_long_after_sweep_structure_break_vwap_and_open():
    b = bars()
    assert scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100) == []  # 25 pts risk: under MIN_RISK
    [s] = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, min_risk=10)
    assert (s["side"], s["time"], s["entry"]) == ("Long", "2026-10-14 11:15", 124)
    assert (s["stop"], s["risk"], s["target"]) == (99, 25, 199)  # last 15m swing low 104 - 5, 3R
    text = message(s, get_settings())
    assert "NIFTY LONG signal at 11:15" in text and "Stop 99.00" in text and "Target 199.00" in text
    assert "Move the stop to entry 124.00 once price reaches 149.00 (+1R)" in text


def test_option_premium_follows_the_index_and_both_charts_render(monkeypatch):
    b = bars()
    [s] = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, min_risk=10)
    plan = {"option": "125 CE", "expiry": "2026-10-19", "iv_pct": 15.0, "forward_basis": 0.0, "entry_premium": 2.0,
            "quote": 2.0, "quantity": 65, "exits": [{"premium": 0.5, "pnl": -100}, {"premium": 50.0, "pnl": 3000}]}  # fmt: skip
    ob = option_bars(b, plan, get_settings())
    assert ob[-1]["close"] > ob[0]["close"]  # index 106 -> 124: the call gains
    assert all(x["low"] <= min(x["open"], x["close"]) <= max(x["open"], x["close"]) <= x["high"] for x in ob)
    levels = [{"label": "PDH", "price": 150.0}, {"label": "R1", "price": 160.0}, {"label": "PDL", "price": 100.0}]
    pngs = charts(b, s, levels, plan, get_settings())
    assert len(pngs) == 2 and all(p.startswith(b"\x89PNG") for p in pngs)
    text = message(s, get_settings(), plan, levels)
    assert "BUY NIFTY 125 CE 2026-10-19" in text and "+3,000 at target" in text
    assert "11:15 structure break: 15m close 124.00 above 15m swing high 120.00 (10:00)" in text
    assert "Stop basis: 15m swing low 104.00 (10:30) - 5" in text and "O 124.00 | H 125.00" in text
    assert "Before target: PDH 150, R1 160 (price may stall there)" in text and "Support: PDL 100" in text
    png = pngs[0]
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(alerts.httpx, "post", lambda url, **kw: calls.append((url, kw)) or Response())
    settings = get_settings().model_copy(update={"alert_telegram_bot_token": "T", "alert_telegram_chat_id": "42"})
    assert alerts.send_photo(settings, "NIFTY LONG", png)
    url, kw = calls[0]
    assert url.endswith("/botT/sendPhoto") and kw["data"]["chat_id"] == "42" and kw["files"]["photo"][1] == png
    assert not alerts.send_photo(get_settings().model_copy(update={"alert_telegram_bot_token": None}), "x", png)


def test_estimated_option_plan_without_a_live_chain():
    settings = get_settings()
    assert weekly_expiry(date(2026, 10, 14), settings) == date(2026, 10, 19)  # Tuesday 20th is Dussehra: Monday
    assert weekly_expiry(date(2026, 10, 22), settings) == date(2026, 10, 27)  # Thursday -> next Tuesday
    assert weekly_expiry(date(2026, 10, 13), settings) == date(2026, 10, 13)  # expiry day itself
    s = {"side": "Long", "time": "2026-10-14 11:15", "entry": 22537.8, "stop": 22475.8, "target": 22661.8,
         "risk": 62.0, "level": 22304.45}  # fmt: skip
    plan = estimated_plan(s, settings, 12.0)
    (stop, target), entry = plan["exits"], plan["entry_premium"]
    assert plan["option"] == "22650 CE" and plan["expiry"]  # 2 strikes above the 22550 ATM == "2026-10-19" and plan["quote"] is None
    assert stop["premium"] < entry < target["premium"] and stop["pnl"] < 0 < target["pnl"]
    assert "estimated at India VIX 12%" in message(s, settings, plan)
    put = estimated_plan({**s, "side": "Short", "stop": 22599.8, "target": 22413.8}, settings, 12.0)
    assert put["option"] == "22450 PE" and put["exits"][1]["premium"] > put["entry_premium"]


def test_backtest_exit_is_stop_first_then_target_then_the_1510_close_with_breakeven_after_1r():
    from app.liquidity_signals import exit_of

    s = {"side": "Long", "time": "2026-10-14 11:15", "entry": 124, "risk": 25, "stop": 99, "target": 174}

    def bar(t, high, low, close=120):
        return {"date": f"2026-10-14 {t}", "high": high, "low": low, "close": close}

    assert exit_of([bar("11:10", 200, 50), bar("11:15", 180, 98)], s) == ("Stop", 99, "2026-10-14 11:20")
    assert exit_of([bar("11:15", 130, 110), bar("11:20", 175, 110)], s) == ("Target", 174, "2026-10-14 11:25")
    assert exit_of([bar("11:15", 130, 110), bar("15:05", 130, 110, 125), bar("15:10", 175, 110)], s) == (
        "15:10", 125, "2026-10-14 15:10")  # fmt: skip
    assert exit_of([bar("11:15", 150, 125), bar("11:20", 130, 120)], s) == ("Breakeven", 124, "2026-10-14 11:25")
    short = {**s, "side": "Short", "stop": 130, "target": 80}
    assert exit_of([bar("11:15", 120, 79)], short) == ("Target", 80, "2026-10-14 11:20")
    assert exit_of([bar("11:15", 125, 98), bar("11:20", 126, 110)], short) == ("Breakeven", 124, "2026-10-14 11:25")


def test_backtest_ledger_books_wins_to_the_wallet_and_sizes_by_capital():
    from scripts.backtest_liquidity import ledger

    def trade(d, t, pin, pout):
        return {"Date": d, "Entry time": t, "Option": "100 CE", "Premium in": pin, "Premium out": pout,
                "Lot size": 50, "Points": pout - pin}  # fmt: skip

    trades = [trade("2026-10-02", "10:00", 100, 160), trade("2026-10-01", "11:00", 100, 80),
              trade("2026-10-02", "12:00", 300, 400)]  # fmt: skip
    days = ledger(trades, ["2026-10-01", "2026-10-02"], get_settings(), 12_000, 2, book=True)
    loss, win, big = trades  # sorted by entry
    assert loss["Date"] == "2026-10-01" and loss["Lots"] == 2 and loss["Option Rs"] < -2_000 == -loss["Booked Rs"] - 2_000
    assert abs(loss["Trading capital Rs"] - (12_000 + loss["Option Rs"])) <= 1
    assert win["Lots"] == 1  # ~9,9xx left pays for one 5,000 lot, not two
    assert win["Option Rs"] > 0 and win["Trading capital Rs"] == 12_000  # the win refills the capital first
    assert abs(win["Booked Rs"] - (win["Option Rs"] + loss["Option Rs"])) <= 1  # and only the rest is booked
    assert big["Lots"] == 0 and big["Option Rs"] == 0  # a 15,000 lot is more than the capital
    assert days[0]["Wallet Rs"] == 0 and days[1]["Wallet Rs"] == win["Booked Rs"]
    assert days[1]["Total Rs"] == days[1]["Trading capital Rs"] + days[1]["Wallet Rs"]


def test_backtest_sizes_by_capital_percent_and_takes_three_days_off_after_three_losses():
    from scripts.backtest_liquidity import ledger

    def trade(d, pout, pstop=80):
        return {"Date": d, "Entry time": "10:00", "Option": "100 CE", "Premium in": 100, "Premium out": pout,
                "Premium at stop": pstop, "Lot size": 50, "Points": pout - 100}  # fmt: skip

    days = [f"2026-10-{n:02d}" for n in range(1, 10)]
    trades = [trade(days[0], 50), trade(days[1], 50), trade(days[2], 90), trade(days[3], 150),
              trade(days[5], 150), trade(days[6], 150), trade(days[7], 150, pstop=0)]  # fmt: skip
    ledger(trades, days, get_settings(), 100_000, 1, book=False, risk_pct=5)
    # 20 x 50 = 1,000 + charges per lot at the stop: 5% of 100,000 buys 4, of ~80,000 after two losses only 3
    assert [t["Lots"] for t in trades[:3]] == [4, 4, 3]
    assert [t["Skipped"] for t in trades[3:]] == ["3-loss break", "3-loss break", "", "1 lot risks over 5% of capital"]
    assert trades[6]["Lots"] == 0  # 5,000+ a lot vs 5% of ~86,000


def test_sweep_stop_sits_past_the_sweep_low():
    b = bars()
    [s] = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, stop_at="sweep")
    assert (s["stop"], s["risk"], s["stop_basis"]) == (93, 31, "sweep low 98.00 - 5")


def test_live_alerts_take_a_break_after_three_losses_in_a_row(monkeypatch):
    import app.liquidity_signals as ls

    monkeypatch.setattr(ls, "scan", lambda *a, **k: [{"side": "Long", "entry": 100.0}])
    monkeypatch.setattr(ls, "exit_of", lambda day, s: ("Stop", 90.0, ""))
    wed_to_fri = [{"date": f"2026-10-{d} 10:00"} for d in ("07", "08", "09")]
    daily = [{"date": "2026-10-01", "high": 1, "low": 0}]
    assert ls.break_until(wed_to_fri, {}, daily, get_settings(), "2026-10-12") == date(2026, 10, 14)  # Mon-Wed off
    assert ls.break_until(wed_to_fri[:2], {}, daily, get_settings(), "2026-10-12") is None


def test_backtest_expiry_follows_nses_schedule():
    from scripts.backtest_liquidity import expiry_for

    trading = {"2018-06-28", "2018-07-26", "2021-01-06", "2025-08-28", "2025-09-02", "2026-10-13"}
    settings = get_settings()
    assert expiry_for(date(2018, 6, 20), trading, settings) == date(2018, 6, 28)  # monthly: last Thursday
    assert expiry_for(date(2018, 6, 29), trading, settings) == date(2018, 7, 26)  # past it: next month's
    assert expiry_for(date(2021, 1, 4), trading, settings) == date(2021, 1, 6)  # weekly Thursday 7th closed
    assert expiry_for(date(2025, 8, 26), trading, settings) == date(2025, 8, 28)  # last Thursday weekly
    assert expiry_for(date(2025, 8, 29), trading, settings) == date(2025, 9, 2)  # Tuesday from September 2025


def test_no_entry_against_the_day_open_or_vwap():
    b = bars(day_open=130)
    assert scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, min_risk=10) == []
    b = bars()
    assert scan(b, {x["date"]: False for x in b}, pdh=200, pdl=100, min_risk=10) == []


def test_staged_exit_candle_close_trail():
    from app.liquidity_signals import staged_exit_of

    s = {"side": "Short", "time": "2026-10-14 11:30", "entry": 200.0, "stop": 220.0, "target": 140.0, "risk": 20.0}

    def bar(t, o, h, l, c):
        return {"date": f"2026-10-14 {t}", "open": o, "high": h, "low": l, "close": c}

    # Reaches 165 (more than +1.5R / 170.0), then next bar closes green (166 -> 172)
    day = [
        bar("11:30", 200, 201, 195, 196),
        bar("11:35", 196, 196, 165, 166),  # reached 165 (down 35 pts = +1.75R)
        bar("11:40", 166, 173, 165, 172),  # closed green -> candle close trail
    ]
    res = staged_exit_of(day, s)
    assert res["exit_reason"] == "Candle Close Trail"
    assert res["exit_price"] == 172.0
    assert res["r_multiple"] == 1.4  # (200 - 172) / 20 = 1.4R


def test_trendline_confluence_and_pool_tag():
    b = bars()
    # Without trendline requirement:
    res_no_tl = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, min_risk=10, require_trendline=False)
    assert len(res_no_tl) == 1
    # With trendline requirement (the sample bars have higher highs/breakout or no opposing line):
    res_tl = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100, min_risk=10, require_trendline=True)
    assert len(res_tl) == 1

    # Check pool tag in alert message
    sig = {**res_tl[0], "pool": "Equal Low (EQL)"}
    msg = message(sig, get_settings())
    assert "sell-side sweep of Equal Low (EQL) 100.00" in msg


