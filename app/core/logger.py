"""
CVG Neuron AI Orchestration System — Structured Logger
Version: 2.0.0 | Clearview Geographic LLC

Provides JSON-structured logging with context binding,
level filtering, and file/console output.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from structlog.types import EventDict, WrappedLogger


# ── Custom processors ─────────────────────────────────────────────────────────

def add_neuron_context(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Inject CVG Neuron identity into every log record."""
    event_dict.setdefault("neuron_id", "CVG-NEURON-001")
    event_dict.setdefault("service", "cvg-neuron")
    return event_dict


def drop_color_message_key(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Remove uvicorn's color_message key (redundant in JSON output)."""
    event_dict.pop("color_message", None)
    return event_dict


# ── Logger factory ────────────────────────────────────────────────────────────

def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure structlog + stdlib logging.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        fmt:   Output format — "json" or "text"
        log_file: Optional path to rotating log file
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure log directory exists
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Build handlers
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
        )

    # Configure root stdlib logger
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
    )

    # Shared processors used in both dev and prod
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        add_neuron_context,
        drop_color_message_key,
    ]

    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    for handler in logging.root.handlers:
        handler.setFormatter(formatter)


def get_logger(name: str = "cvg-neuron", **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """
    Get a bound logger with optional initial context.

    Usage:
        log = get_logger("orchestrator", task_id="abc123")
        log.info("Task started", agent="nlp-agent")
    """
    return structlog.get_logger(name).bind(**initial_context)


class NeuronLogger:
    """
    Context-manager / class-based helper for component logging.
    Binds component name and optional task context automatically.
    """

    def __init__(self, component: str, **context: Any) -> None:
        self._logger = get_logger(component, component=component, **context)

    def bind(self, **kwargs: Any) -> "NeuronLogger":
        self._logger = self._logger.bind(**kwargs)
        return self

    def debug(self, event: str, **kw: Any) -> None:
        self._logger.debug(event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._logger.info(event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._logger.warning(event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._logger.error(event, **kw)

    def critical(self, event: str, **kw: Any) -> None:
        self._logger.critical(event, **kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._logger.exception(event, **kw)
