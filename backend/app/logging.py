"""Compatibility alias module forwarding to app.logger."""

from app.logger import (
    DEFAULT_DATEFMT,
    DEFAULT_FORMAT,
    LOGGABLE_LEVELS,
    SUPPRESSED_LOGGERS,
    RequestLoggingMiddleware,
    configure_logging,
    get_logger,
)

__all__ = [
    "DEFAULT_DATEFMT",
    "DEFAULT_FORMAT",
    "LOGGABLE_LEVELS",
    "SUPPRESSED_LOGGERS",
    "RequestLoggingMiddleware",
    "configure_logging",
    "get_logger",
]
