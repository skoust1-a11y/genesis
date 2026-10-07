from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["openai", "anthropic"] = Field(default="openai", alias="LLM_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5-5", alias="ANTHROPIC_MODEL")

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
    # Common risk rules for every strategy, in backtests and live; a strategy's own value wins only when stricter.
    # 0 disables each one. The losing streak and its cooldown go together.
    max_entries_per_day: int = Field(default=0, ge=0, alias="MAX_ENTRIES_PER_DAY")
    max_consecutive_losses: int = Field(default=0, ge=0, alias="MAX_CONSECUTIVE_LOSSES")
    loss_cooldown_days: int = Field(default=0, ge=0, le=30, alias="LOSS_COOLDOWN_DAYS")
    max_loss_pct_of_capital: float = Field(default=0, ge=0, le=100, alias="MAX_LOSS_PCT_OF_CAPITAL")
    # Fallback when the data has no lot size (NSE/FAOP/70616: from the January 2026 expiries)
    lot_sizes: str = Field(default="NIFTY:65,BANKNIFTY:30", alias="LOT_SIZES")
    strike_steps: str = Field(default="NIFTY:50,BANKNIFTY:100", alias="STRIKE_STEPS")
    max_snapshot_age_seconds: int = Field(default=120, alias="MAX_SNAPSHOT_AGE_SECONDS")
    trade_crew_explain: bool = Field(default=False, alias="TRADE_CREW_EXPLAIN")

    # Brokers and live orders
    live_trading_enabled: bool = Field(default=False, alias="LIVE_TRADING_ENABLED")
    sandbox_allow_live: bool = Field(default=False, alias="SANDBOX_ALLOW_LIVE")
    sandbox_state_dir: str | None = Field(default=None, alias="SANDBOX_STATE_DIR")  # default: DATA_DIR/sandbox
    live_min_paper_days: int = Field(default=0, ge=0, alias="LIVE_MIN_PAPER_DAYS")
    live_allowed_structures: str = Field(
        default="iron_condor,long_call,long_put,bull_call_spread,bear_put_spread", alias="LIVE_ALLOWED_STRUCTURES"
    )
    broker_credentials_key: str | None = Field(default=None, alias="BROKER_CREDENTIALS_KEY")
    max_spread_pct: float = Field(default=5, gt=0, alias="MAX_SPREAD_PCT")
    order_fill_timeout_s: float = Field(default=30, gt=0, alias="ORDER_FILL_TIMEOUT_S")
    order_poll_interval_s: float = Field(default=1, gt=0, alias="ORDER_POLL_INTERVAL_S")
    order_reprice_steps: int = Field(default=2, ge=0, alias="ORDER_REPRICE_STEPS")
    order_exit_retries: int = Field(default=2, ge=0, alias="ORDER_EXIT_RETRIES")
    max_slippage_bps: float = Field(default=100, ge=0, alias="MAX_SLIPPAGE_BPS")
    # 0 disables the per-order value cap.
    max_order_value_inr: float = Field(default=0, ge=0, alias="MAX_ORDER_VALUE_INR")
    max_orders_per_minute: int = Field(default=120, ge=1, alias="MAX_ORDERS_PER_MINUTE")
    freeze_quantities: str = Field(default="NIFTY:1800,BANKNIFTY:900", alias="FREEZE_QUANTITIES")
    market_open: str = Field(default="09:15", alias="MARKET_OPEN")
    market_close: str = Field(default="15:30", alias="MARKET_CLOSE")
    # off | live | always: broker-side stop-limit backstops on short legs after an entry.
    protective_stops: Literal["off", "live", "always"] = Field(default="live", alias="PROTECTIVE_STOPS")
    protective_stop_multiplier: float = Field(default=1.5, gt=0, alias="PROTECTIVE_STOP_MULTIPLIER")
    protective_stop_limit_buffer_pct: float = Field(default=5, ge=0, alias="PROTECTIVE_STOP_LIMIT_BUFFER_PCT")
    margin_buffer: float = Field(default=1.2, ge=1, alias="MARGIN_BUFFER")
    heartbeat_max_age_s: int = Field(default=180, gt=0, alias="HEARTBEAT_MAX_AGE_S")

    # Alerts (each channel is used only when configured)
    alert_telegram_bot_token: str | None = Field(default=None, alias="ALERT_TELEGRAM_BOT_TOKEN")
    alert_telegram_chat_id: str | None = Field(default=None, alias="ALERT_TELEGRAM_CHAT_ID")
    alert_smtp_host: str | None = Field(default=None, alias="ALERT_SMTP_HOST")
    alert_smtp_port: int = Field(default=587, alias="ALERT_SMTP_PORT")
    alert_smtp_user: str | None = Field(default=None, alias="ALERT_SMTP_USER")
    alert_smtp_password: str | None = Field(default=None, alias="ALERT_SMTP_PASSWORD")
    alert_smtp_from: str | None = Field(default=None, alias="ALERT_SMTP_FROM")
    alert_smtp_to: str | None = Field(default=None, alias="ALERT_SMTP_TO")

    # API auth: comma-separated `name:sha256(token)` pairs. Empty leaves research and paper flows open.
    api_tokens: str = Field(default="", alias="API_TOKENS")

    backend_port: int = Field(default=8841, alias="BACKEND_PORT")
    cors_origins: str = Field(default="http://127.0.0.1:8842", alias="CORS_ORIGINS")

    @model_validator(mode="after")
    def _streak_and_cooldown(self) -> "Settings":
        if bool(self.max_consecutive_losses) != bool(self.loss_cooldown_days):
            raise ValueError("MAX_CONSECUTIVE_LOSSES and LOSS_COOLDOWN_DAYS go together: set both, or both to 0")
        return self

    @property
    def common_risk(self) -> dict[str, float]:
        """The common risk rules that are switched on, as `risk:` keys."""
        values = {
            "max_entries_per_day": self.max_entries_per_day,
            "max_consecutive_losses": self.max_consecutive_losses,
            "cooldown_days": self.loss_cooldown_days,
            "max_loss_pct_of_capital": self.max_loss_pct_of_capital,
        }
        return {k: v for k, v in values.items() if v}

    @property
    def llm_api_key(self) -> str | None:
        return self.anthropic_api_key if self.llm_provider == "anthropic" else self.openai_api_key

    @property
    def llm_model(self) -> str:
        return self.anthropic_model if self.llm_provider == "anthropic" else self.openai_model

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @staticmethod
    def _per_underlying(raw: str) -> dict[str, float]:
        pairs = (item.split(":", 1) for item in raw.split(",") if ":" in item)
        return {k.strip().upper(): float(v) for k, v in pairs}

    def lot_size(self, underlying: str) -> int | None:
        value = self._per_underlying(self.lot_sizes).get(underlying.upper())
        return int(value) if value else None

    def strike_step(self, underlying: str) -> float | None:
        return self._per_underlying(self.strike_steps).get(underlying.upper())

    def freeze_quantity(self, underlying: str) -> int | None:
        value = self._per_underlying(self.freeze_quantities).get(underlying.upper())
        return int(value) if value else None

    @property
    def allowed_live_structures(self) -> set[str]:
        return {s.strip() for s in self.live_allowed_structures.split(",") if s.strip()}

    @property
    def token_hashes(self) -> dict[str, str]:
        """sha256 hex -> actor name, from API_TOKENS."""
        pairs = (item.split(":", 1) for item in self.api_tokens.split(",") if ":" in item)
        return {h.strip().lower(): name.strip() for name, h in pairs if name.strip() and h.strip()}

    @property
    def opinion_model(self) -> str:
        return self.llm_opinion_model or self.llm_model

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
