from app import alerts
from app.config import get_settings
from app.liquidity_signals import charts, message, option_bars, scan

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
    [s] = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100)
    assert (s["side"], s["time"], s["entry"]) == ("Long", "2026-10-14 11:15", 124)
    assert (s["stop"], s["risk"], s["target"]) == (99, 25, 174)  # last 15m swing low 104 - 5, 2R
    text = message(s, get_settings())
    assert "NIFTY LONG signal at 11:15" in text and "Stop 99.00" in text and "Target 174.00" in text


def test_option_premium_follows_the_index_and_both_charts_render(monkeypatch):
    b = bars()
    [s] = scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100)
    plan = {"option": "125 CE", "expiry": "2026-10-19", "iv_pct": 15.0, "forward_basis": 0.0, "entry_premium": 2.0,
            "quote": 2.0, "quantity": 65, "exits": [{"premium": 0.5, "pnl": -100}, {"premium": 50.0, "pnl": 3000}]}  # fmt: skip
    ob = option_bars(b, plan, get_settings())
    assert ob[-1]["close"] > ob[0]["close"]  # index 106 -> 124: the call gains
    assert all(x["low"] <= min(x["open"], x["close"]) <= max(x["open"], x["close"]) <= x["high"] for x in ob)
    pngs = charts(b, s, 200, 100, plan, get_settings())
    assert len(pngs) == 2 and all(p.startswith(b"\x89PNG") for p in pngs)
    levels = [{"label": "PDH", "price": 150.0}, {"label": "R1", "price": 160.0}, {"label": "PDL", "price": 100.0}]
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


def test_no_entry_against_the_day_open_or_vwap():
    b = bars(day_open=130)
    assert scan(b, {x["date"]: True for x in b}, pdh=200, pdl=100) == []
    b = bars()
    assert scan(b, {x["date"]: False for x in b}, pdh=200, pdl=100) == []
