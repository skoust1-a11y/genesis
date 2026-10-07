from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    data_dir: str = Field(default="data", alias="DATA_DIR")
    strategies_dir: str = Field(default="data/strategies", alias="STRATEGIES_DIR")
    reports_dir: str = Field(default="data/reports", alias="REPORTS_DIR")

    default_capital_inr: float = Field(default=500_000, alias="DEFAULT_CAPITAL_INR")
    default_max_lots: int = Field(default=2, alias="DEFAULT_MAX_LOTS")
    max_scenario_combos: int = Field(default=24, alias="MAX_SCENARIO_COMBOS")

    brokerage_per_order: float = Field(default=20, alias="BROKERAGE_PER_ORDER")
    stt_pct: float = Field(default=0.0005, alias="STT_PCT")
    exchange_txn_pct: float = Field(default=0.0003503, alias="EXCHANGE_TXN_PCT")
    gst_pct: float = Field(default=0.18, alias="GST_PCT")
    sebi_per_crore: float = Field(default=10, alias="SEBI_PER_CRORE")
    stamp_duty_pct: float = Field(default=0.00003, alias="STAMP_DUTY_PCT")
    slippage_bps: float = Field(default=5, alias="SLIPPAGE_BPS")

    backend_port: int = Field(default=8841, alias="BACKEND_PORT")
    cors_origins: str = Field(default="http://127.0.0.1:8842", alias="CORS_ORIGINS")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
