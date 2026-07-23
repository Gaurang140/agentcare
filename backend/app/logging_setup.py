"""Structured logging setup (structlog): JSON in prod, console in dev."""

import logging
import sys
from typing import Any

import structlog

from app.config import settings

_SENSITIVE_MARKERS = ("password", "token", "authorization", "api_key", "secret")


def _is_sensitive_key(key: Any) -> bool:
    return any(marker in str(key).lower() for marker in _SENSITIVE_MARKERS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _is_sensitive_key(key) else _redact_value(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Recursively redact values of sensitive keys in the event dict.

    A key matches (case-insensitively) if it contains one of: password,
    token, authorization, api_key, secret.
    """
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = "[redacted]"
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def configure_logging() -> None:
    """Configure structlog. JSON renderer outside dev, console renderer in dev."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]

    if settings.environment != "dev":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Return a structlog bound logger for the given name."""
    return structlog.get_logger(name)
