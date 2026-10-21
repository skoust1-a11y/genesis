"""Unit tests for structured logging configuration, rotation, subsystem routing, and middleware."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.logger import (
    DEFAULT_DATEFMT,
    DEFAULT_FORMAT,
    LOGGABLE_LEVELS,
    SUPPRESSED_LOGGERS,
    configure_logging,
    get_logger,
)
from app.main import app


def test_loggable_levels_and_defaults() -> None:
    assert "DEBUG" in LOGGABLE_LEVELS
    assert "INFO" in LOGGABLE_LEVELS
    assert "WARNING" in LOGGABLE_LEVELS
    assert "ERROR" in LOGGABLE_LEVELS
    assert "%(asctime)s" in DEFAULT_FORMAT
    assert DEFAULT_DATEFMT == "%Y-%m-%d %H:%M:%S"


def test_settings_log_level_validation() -> None:
    s_debug = Settings(LOG_LEVEL="debug")
    assert s_debug.log_level == "DEBUG"

    s_warn = Settings(LOG_LEVEL="warning")
    assert s_warn.log_level == "WARNING"

    s_invalid = Settings(LOG_LEVEL="nonexistent_level")
    assert s_invalid.log_level == "INFO"


def test_get_logger_helper() -> None:
    default_log = get_logger()
    assert default_log.name == "genesis"

    custom_log = get_logger("app.custom")
    assert custom_log.name == "app.custom"


def test_configure_logging_accepts_invalid_level() -> None:
    # Should not raise exception and defaults safely to INFO
    configure_logging(log_level="NOT_A_VALID_LEVEL", log_file="")
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_console_only() -> None:
    configure_logging(log_level="DEBUG", log_file="")
    root = logging.getLogger()
    # No file handlers on root when log_file is disabled
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 0


def test_configure_logging_with_file_and_subsystems(tmp_path: Path) -> None:
    app_log = tmp_path / "logs" / "app.log"
    errors_log = tmp_path / "logs" / "errors.log"
    live_log = tmp_path / "logs" / "live.log"
    signals_log = tmp_path / "logs" / "signals.log"
    alerts_log = tmp_path / "logs" / "alerts.log"
    engine_log = tmp_path / "logs" / "engine.log"
    providers_log = tmp_path / "logs" / "providers.log"
    api_log = tmp_path / "logs" / "api.log"

    configure_logging(log_level="DEBUG", log_file=str(app_log))

    # Emit logs across various system categories
    logging.getLogger("app").info("Unified app info message")
    logging.getLogger("genesis.live").info("Trade crew tick order fill")
    logging.getLogger("app.liquidity_signals").info("Liquidity sweep detected at PDH")
    logging.getLogger("genesis.alerts").info("Telegram signal dispatch succeeded")
    logging.getLogger("app.engine").info("Backtest simulation run completed")
    logging.getLogger("app.llm").info("LLM query to Groq completed")
    logging.getLogger("app.request").info("FastAPI endpoint served")
    logging.getLogger("app").warning("Suspicious slippage detected warning")
    logging.getLogger("app").error("Critical database connection error")

    # Flush all handlers
    for h in logging.getLogger().handlers:
        h.flush()
    for name in [
        "genesis.live",
        "app.liquidity_signals",
        "genesis.alerts",
        "app.engine",
        "app.llm",
        "app.request",
    ]:
        for h in logging.getLogger(name).handlers:
            h.flush()

    # Verify unified app.log
    assert app_log.exists()
    content_app = app_log.read_text(encoding="utf-8")
    assert "Unified app info message" in content_app
    assert "Trade crew tick order fill" in content_app
    assert "Suspicious slippage detected warning" in content_app
    assert "Critical database connection error" in content_app

    # Verify errors.log captures WARNING and ERROR only
    assert errors_log.exists()
    content_errors = errors_log.read_text(encoding="utf-8")
    assert "Suspicious slippage detected warning" in content_errors
    assert "Critical database connection error" in content_errors
    assert "Unified app info message" not in content_errors

    # Verify subsystem routing
    assert live_log.exists()
    assert "Trade crew tick order fill" in live_log.read_text(encoding="utf-8")

    assert signals_log.exists()
    assert "Liquidity sweep detected at PDH" in signals_log.read_text(encoding="utf-8")

    assert alerts_log.exists()
    assert "Telegram signal dispatch succeeded" in alerts_log.read_text(encoding="utf-8")

    assert engine_log.exists()
    assert "Backtest simulation run completed" in engine_log.read_text(encoding="utf-8")

    assert providers_log.exists()
    assert "LLM query to Groq completed" in providers_log.read_text(encoding="utf-8")

    assert api_log.exists()
    assert "FastAPI endpoint served" in api_log.read_text(encoding="utf-8")


def test_suppressed_third_party_loggers() -> None:
    configure_logging(log_level="DEBUG", log_file="")
    for name in SUPPRESSED_LOGGERS:
        log = logging.getLogger(name)
        assert log.level >= logging.WARNING


def test_request_logging_middleware(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    configure_logging(log_level="INFO", log_file=str(log_file))

    client = TestClient(app)
    response = client.get("/api/notifications")
    assert response.status_code in {200, 401}  # Valid response from route

    for h in logging.getLogger().handlers:
        h.flush()
    for h in logging.getLogger("app.request").handlers:
        h.flush()

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "GET /api/notifications ->" in content
