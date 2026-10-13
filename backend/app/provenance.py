from __future__ import annotations

import subprocess
from dataclasses import asdict
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from app.agents.review_crew.reviewer import REVIEW_RULES_VERSION
from app.config import Settings
from app.engine.costs import resolve_cost_profile
from app.engine.market import BACKEND_ROOT, market_data_fingerprint
from app.engine.sim import ENGINE_VERSION
from app.strategies.schema import StrategyDocument


@lru_cache
def app_version() -> str:
    try:
        return version("trading-strategy-lab-backend")
    except PackageNotFoundError:
        return "unknown"


@lru_cache
def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def report_inputs(
    settings: Settings, strategy: StrategyDocument, strategy_version_id: int, content_sha256: str
) -> dict:
    """Everything needed to reproduce a report. Never includes secrets."""
    return {
        "strategy_version_id": strategy_version_id,
        "content_sha256": content_sha256,
        "cost_profile": asdict(resolve_cost_profile(settings, strategy)),
        "settings_snapshot": {
            "default_capital_inr": settings.default_capital_inr,
            "default_max_lots": settings.default_max_lots,
            "max_scenario_combos": settings.max_scenario_combos,
            "verdict_min_backtest_trades": settings.verdict_min_backtest_trades,
            "verdict_min_forward_trades": settings.verdict_min_forward_trades,
            "iv_rank_lookback_days": settings.iv_rank_lookback_days,
            "portfolio_limits": {
                "max_open_positions": settings.portfolio_max_open_positions,
                "max_daily_loss_inr": settings.portfolio_max_daily_loss_inr,
            },
            "common_risk": settings.common_risk,
            "llm_enabled": settings.llm_enabled,
            "llm_provider": settings.active_llm_provider if settings.llm_enabled else None,
            "llm_model": settings.llm_model if settings.llm_enabled else None,
        },
        "market_data": market_data_fingerprint(strategy.rules.instrument.underlying if strategy.rules else None),
        "engine_version": ENGINE_VERSION,
        "review_rules_version": REVIEW_RULES_VERSION,
        "app_version": app_version(),
        "git_commit": git_commit(),
    }
