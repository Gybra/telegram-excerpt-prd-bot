"""Structured logging configuration via structlog.

In polling mode (local dev) emits colored console output, human-readable.
In webhook mode (production/Cloud Run) emits JSON on stdout, optimal for
Cloud Logging ingestion.

Example:
    >>> from telegram_excerpt.logging_conf import configure_logging, get_logger
    >>> configure_logging(json_output=False)
    >>> log = get_logger(__name__)
    >>> log.info("bot.registered", bot_chat_id=-100123, n=50)
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor


def configure_logging(*, json_output: bool, level: str = "INFO") -> None:
    """Configure structlog and stdlib logging.

    Args:
        json_output: If True emit JSON lines (for Cloud Logging).
                     If False emit colored output (dev).
        level: Minimum log level (DEBUG|INFO|WARNING|ERROR).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Align stdlib logging (e.g. uvicorn, PTB) with structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    # Silence noisy loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the module name."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
