"""Structured logging configuration with rotating file handler."""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler


class _JsonFormatter(logging.Formatter):
    """Produce valid JSON log lines regardless of message content.

    Uses ``json.dumps`` to properly escape quotes, backslashes, newlines,
    and other special characters that would break hand-crafted JSON templates.
    """

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # Let the base class handle %(message)s interpolation and exception info.
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{record.stack_info}"
        return json.dumps(
            {
                "time": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            },
            ensure_ascii=False,
        )


def _resolve_log_level(log_level: str) -> int:
    """Convert a configured log-level name into a stdlib logging level."""
    normalized = log_level.strip().upper()
    level = getattr(logging, normalized, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid SCRAPEYARD_LOG_LEVEL: {log_level!r}")
    return level


def setup_logging(log_dir: str, log_level: str = "INFO") -> None:
    """Configure structured JSON logging once per process."""
    root = logging.getLogger()
    if getattr(root, "_scrapeyard_logging_initialized", False):
        return

    os.makedirs(log_dir, exist_ok=True)

    fmt = _JsonFormatter()
    root.setLevel(_resolve_log_level(log_level))

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "scrapeyard.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    setattr(root, "_scrapeyard_logging_initialized", True)  # noqa: B010
