from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    data_dir: str = Field(default="data", alias="DATA_DIR")
    database_url: str = Field(default="sqlite:///data/genesis.db", alias="DATABASE_URL")
    live_database_url: str = Field(default="sqlite:///data/genesis_live.db", alias="LIVE_DATABASE_URL")
    # Imported NSE end-of-day data, events and news. Reference data, outside the audited databases.
    market_database_url: str = Field(default="sqlite:///data/market_eod.db", alias="MARKET_DATABASE_URL")

    default_capital_inr: float = Field(default=500_000, alias="DEFAULT_CAPITAL_INR")
    default_max_lots: int = Field(default=2, alias="DEFAULT_MAX_LOTS")
    max_scenario_combos: int = Field(default=24, alias="MAX_SCENARIO_COMBOS")
    verdict_min_backtest_trades: int = Field(default=3, alias="VERDICT_MIN_BACKTEST_TRADES")
    verdict_min_forward_trades: int = Field(default=2, alias="VERDICT_MIN_FORWARD_TRADES")
    iv_rank_lookback_days: int = Field(default=252, alias="IV_RANK_LOOKBACK_DAYS")
    iv_rank_min_history: int = Field(default=20, alias="IV_RANK_MIN_HISTORY")

    brokerage_per_order: float = Field(default=20, alias="BROKERAGE_PER_ORDER")
    stt_pct: float = Field(default=0.0005, alias="STT_PCT")
    exchange_txn_pct: float = Field(default=0.0003503, alias="EXCHANGE_TXN_PCT")
    gst_pct: float = Field(default=0.18, alias="GST_PCT")
    sebi_per_crore: float = Field(default=10, alias="SEBI_PER_CRORE")
    stamp_duty_pct: float = Field(default=0.00003, alias="STAMP_DUTY_PCT")
    slippage_bps: float = Field(default=5, alias="SLIPPAGE_BPS")
    # "effective-date:rate" pairs used when a trade date is known; the flat rates above apply otherwise.
    # Verify against current NSE / Finance Act rates before relying on them.
    stt_schedule: str = Field(default="2000-01-01:0.000625,2024-10-01:0.001", alias="STT_SCHEDULE")
    exchange_txn_schedule: str = Field(
        default="2000-01-01:0.000503,2024-10-01:0.0003503", alias="EXCHANGE_TXN_SCHEDULE"
    )
    exercise_stt_pct: float = Field(default=0.00125, alias="EXERCISE_STT_PCT")

    # Events and news (MarketContext)
    event_lookahead_days: int = Field(default=14, alias="EVENT_LOOKAHEAD_DAYS")
    event_lookback_days: int = Field(default=7, alias="EVENT_LOOKBACK_DAYS")
    news_feeds: str = Field(
        default=(
            "moneycontrol=https://www.moneycontrol.com/rss/marketreports.xml,"
            "economictimes=https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms,"
            "livemint=https://www.livemint.com/rss/markets,"
            "business_standard=https://www.business-standard.com/rss/markets-106.rss"
        ),
        alias="NEWS_FEEDS",
    )
    news_lookback_hours: int = Field(default=24, alias="NEWS_LOOKBACK_HOURS")
    news_max_items: int = Field(default=20, alias="NEWS_MAX_ITEMS")

    # LLM trade reviewer: off | shadow | veto. Veto still needs per-deployment evidence and approval.
    llm_trade_opinion: Literal["off", "shadow", "veto"] = Field(default="off", alias="LLM_TRADE_OPINION")
    llm_opinion_model: str | None = Field(default=None, alias="LLM_OPINION_MODEL")
    llm_opinion_timeout_s: float = Field(default=20, alias="LLM_OPINION_TIMEOUT_S")
    llm_opinion_lessons: int = Field(default=5, alias="LLM_OPINION_LESSONS")
    llm_veto_live_allowed: bool = Field(default=False, alias="LLM_VETO_LIVE_ALLOWED")
    opinion_eval_min_samples: int = Field(default=30, alias="OPINION_EVAL_MIN_SAMPLES")
    opinion_eval_window: int = Field(default=50, alias="OPINION_EVAL_WINDOW")

    # Live trading (Trade Crew)
    portfolio_max_open_positions: int = Field(default=3, alias="PORTFOLIO_MAX_OPEN_POSITIONS")
    portfolio_max_daily_loss_inr: float = Field(default=10_000, alias="PORTFOLIO_MAX_DAILY_LOSS_INR")
    # Drawdown from peak equity at which every deployment pauses, whatever its own ladder says. 0 disables it.
    max_drawdown_pct: float = Field(default=20, ge=0, le=100, alias="MAX_DRAWDOWN_PCT")
    lot_sizes: str = Field(default="NIFTY:75,BANKNIFTY:35", alias="LOT_SIZES")
    strike_steps: str = Field(default="NIFTY:50,BANKNIFTY:100", alias="STRIKE_STEPS")
    max_snapshot_age_seconds: int = Field(default=120, alias="MAX_SNAPSHOT_AGE_SECONDS")
    trade_crew_explain: bool = Field(default=False, alias="TRADE_CREW_EXPLAIN")

    backend_port: int = Field(default=8841, alias="BACKEND_PORT")
    cors_origins: str = Field(default="http://127.0.0.1:8842", alias="CORS_ORIGINS")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @staticmethod
    def _per_underlying(raw: str) -> dict[str, float]:
        pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
        return {k.strip().upper(): float(v) for k, v in pairs}

    def lot_size(self, underlying: str) -> int | None:
        value = self._per_underlying(self.lot_sizes).get(underlying.upper())
        return int(value) if value else None

    def strike_step(self, underlying: str) -> float | None:
        return self._per_underlying(self.strike_steps).get(underlying.upper())

    @property
    def opinion_model(self) -> str:
        return self.llm_opinion_model or self.openai_model

    @staticmethod
    def schedule(raw: str) -> list[tuple[str, float]]:
        pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
        return sorted((k.strip(), float(v)) for k, v in pairs)

    def news_feed_urls(self) -> dict[str, str]:
        pairs = (item.split("=", 1) for item in self.news_feeds.split(",") if "=" in item)
        return {k.strip(): v.strip() for k, v in pairs}


@lru_cache
def get_settings() -> Settings:
    return Settings()
