"""Process-wide structured logging configuration with rotation and subsystem routing."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from logging.config import dictConfig
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.audit.context import current_actor, current_request_id
from app.config import Settings, get_settings

LOGGABLE_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are verbose at INFO or DEBUG
SUPPRESSED_LOGGERS = (
    "openai",
    "httpx",
    "httpcore",
    "google_genai",
    "google.genai",
    "langfuse",
    "urllib3",
    "watchfiles",
    "asyncio",
)


def configure_logging(
    settings: Settings | None = None,
    *,
    log_level: str | None = None,
    log_file: str | None = None,
) -> None:
    """Configure stdout and structured category-specific rotating log files."""
    resolved_settings = settings or get_settings()

    level = (log_level or getattr(resolved_settings, "log_level", "INFO") or "INFO").strip().upper()
    if level not in LOGGABLE_LEVELS:
        level = "INFO"

    formatter_config = {
        "default": {
            "format": DEFAULT_FORMAT,
            "datefmt": DEFAULT_DATEFMT,
        }
    }

    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        }
    }
    root_handlers = ["console"]
    logger_configs: dict[str, dict[str, Any]] = {
        "genesis": {"level": level, "propagate": True},
        "app": {"level": level, "propagate": True},
    }

    target_log_file = log_file if log_file is not None else getattr(resolved_settings, "log_file", "logs/app.log")
    target_log_file = (target_log_file or "").strip()

    if target_log_file:
        file_path = Path(target_log_file)
        log_dir = file_path.parent
        log_dir.mkdir(parents=True, exist_ok=True)

        # 1. Main Unified Log File (all logs at the configured level)
        handlers["file_app"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "default",
            "filename": str(file_path),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "encoding": "utf-8",
        }
        root_handlers.append("file_app")

        # 2. Errors & Warnings Dedicated Log File (WARNING and ERROR across entire system)
        handlers["file_errors"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "default",
            "filename": str(log_dir / "errors.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "level": "WARNING",
        }
        root_handlers.append("file_errors")

        # 3. Subsystem Rotating Log Files
        subsystems = {
            "live": {
                "file": "live.log",
                "loggers": ["genesis.live", "genesis.supervisor", "app.live"],
            },
            "signals": {
                "file": "signals.log",
                "loggers": [
                    "app.liquidity_signals",
                    "app.equal_signals",
                    "app.intraday_liquidity",
                    "genesis.signals",
                ],
            },
            "alerts": {
                "file": "alerts.log",
                "loggers": ["genesis.alerts", "app.alerts"],
            },
            "engine": {
                "file": "engine.log",
                "loggers": ["app.engine", "app.strategies", "app.journal"],
            },
            "providers": {
                "file": "providers.log",
                "loggers": ["app.llm", "app.market", "genesis.providers"],
            },
            "api": {
                "file": "api.log",
                "loggers": ["app.request", "app.api"],
            },
        }

        for sub_name, sub_cfg in subsystems.items():
            handler_key = f"file_{sub_name}"
            handlers[handler_key] = {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "default",
                "filename": str(log_dir / sub_cfg["file"]),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
            }
            for l_name in sub_cfg["loggers"]:
                logger_configs[l_name] = {
                    "level": level,
                    "handlers": [handler_key],
                    "propagate": True,
                }

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": formatter_config,
            "handlers": handlers,
            "root": {"level": level, "handlers": root_handlers},
            "loggers": logger_configs,
        }
    )

    # Suppress verbose third-party loggers
    for noisy in SUPPRESSED_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("genesis").debug(
        "Logging configured at %s (file=%s)", level, target_log_file or "disabled"
    )


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger configured under the genesis namespace or caller's name."""
    return logging.getLogger(name or "genesis")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log incoming HTTP request method, path, status code, latency, actor, and request ID."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        logger = logging.getLogger("app.request")
        start = time.perf_counter()
        req_id = current_request_id() or request.headers.get("x-request-id") or "-"
        actor = current_actor()

        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000
            final_req_id = (
                response.headers.get("x-request-id")
                or current_request_id()
                or req_id
            )
            logger.info(
                "%s %s -> %s (%.1f ms) [req=%s actor=%s]",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                final_req_id,
                actor,
            )
            return response
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "%s %s -> ERROR (%.1f ms) [req=%s actor=%s]",
                request.method,
                request.url.path,
                duration_ms,
                req_id,
                actor,
            )
            raise
