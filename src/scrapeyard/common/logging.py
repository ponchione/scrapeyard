"""Structured logging configuration with rotating file handler."""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str) -> None:
    """Configure structured JSON logging once per process."""
    root = logging.getLogger()
    if getattr(root, "_scrapeyard_logging_initialized", False):
        return

    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root.setLevel(logging.INFO)

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

    setattr(root, "_scrapeyard_logging_initialized", True)
