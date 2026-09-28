"""Structured logging: modules use `get_logger(__name__)`; the CLI calls `configure_logging`."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import TextIO, cast

import structlog
from structlog.typing import FilteringBoundLogger

from ebs.core.errors import ConfigError

# structlog.get_logger() reserves the `logger` keyword, so the name travels under this key and
# is renamed at render time. Binding it instead would freeze the configuration at import time.
_NAME_KEY = "_ebs_logger"


def _add_logger_name(
    _logger: object, _method: str, event_dict: MutableMapping[str, object]
) -> MutableMapping[str, object]:
    if _NAME_KEY in event_dict:
        event_dict["logger"] = event_dict.pop(_NAME_KEY)
    return event_dict


def _parse_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    numeric = logging.getLevelNamesMapping().get(level.strip().upper())
    if numeric is None:
        raise ConfigError(
            f"unknown log level {level!r}; use one of DEBUG, INFO, WARNING, ERROR, CRITICAL"
        )
    return numeric


def configure_logging(
    level: int | str = logging.INFO,
    *,
    json: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog for the process: JSON lines for machines, key=value for humans."""
    numeric_level = _parse_level(level)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    """A logger bound to `name` (use the module's `__name__`)."""
    return cast(FilteringBoundLogger, structlog.get_logger(**{_NAME_KEY: name}))
