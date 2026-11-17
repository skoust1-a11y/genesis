import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from app.trading.forward_health import ScanCoverage, baseline_issues
from app.trading.option_quote import IST

OPEN = datetime(2026, 11, 16, 9, 15, tzinfo=IST)


def test_late_start_and_restart_gaps_remain_visible_after_healthy_ticks(tmp_path):
    coverage = ScanCoverage(tmp_path)
    first = coverage.complete(OPEN + timedelta(minutes=19), ["NEWS_UNAVAILABLE"])
    assert first["gap_count"] == 1 and first["max_gap_seconds"] == 1140
    restarted = ScanCoverage(tmp_path)
    resumed = restarted.complete(OPEN + timedelta(minutes=64), [])
    assert resumed["gap_count"] == 2 and not resumed["continuous_coverage"]
    assert resumed["current_issues"] == []
    healthy = restarted.complete(OPEN + timedelta(minutes=64, seconds=10), [])
    assert healthy["gap_count"] == 2
    assert len((tmp_path / "coverage-incidents.jsonl").read_text().splitlines()) == 2


def test_preopen_overnight_and_regular_cadence_are_not_outages(tmp_path):
    coverage = ScanCoverage(tmp_path)
    coverage.complete(OPEN - timedelta(minutes=5), [])
    assert coverage.complete(OPEN + timedelta(seconds=10), [])["continuous_coverage"]
    assert coverage.complete(OPEN + timedelta(seconds=20), [])["gap_count"] == 0
    tomorrow = OPEN + timedelta(days=1, seconds=5)
    assert coverage.complete(tomorrow, [])["gap_count"] == 0


def test_installation_on_existing_run_uses_last_known_completion(tmp_path):
    prior = OPEN + timedelta(hours=4)
    (tmp_path / "status.json").write_text(json.dumps({"updated_at": prior.isoformat()}))
    coverage = ScanCoverage(tmp_path)
    result = coverage.complete(prior + timedelta(seconds=10), [])
    assert result["gap_count"] == 0
    assert "earlier scans are not audited" in result["scope"]


def test_input_health_distinguishes_absent_chain_quotes_and_candle_blocks():
    view = SimpleNamespace(chain={"contracts": []}, df_5m=pd.DataFrame(),
                           rejections={"5m": ["RECENT_NEWS_UNAVAILABLE"]})
    issues = baseline_issues(view)
    assert "BASELINE_OPTION_CHAIN_EMPTY" in issues
    assert "BASELINE_CLOSED_5M_UNAVAILABLE" in issues
    assert "BASELINE_5M:RECENT_NEWS_UNAVAILABLE" in issues
    view.chain = {"contracts": [{"ce": {"tradingSymbol": "test"}}]}
    assert "BASELINE_EXECUTABLE_QUOTES_UNAVAILABLE" in baseline_issues(view)
    view.chain["contracts"][0]["ce"]["liveData"] = {"bid": 20, "ask": 20.05}
    view.df_5m = pd.DataFrame([{"close": 25000}])
    view.rejections = {"15m": ["INVALID_OR_STALE_CANDLES"]}
    assert baseline_issues(view) == []
