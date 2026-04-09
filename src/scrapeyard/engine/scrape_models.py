"""Shared scraper result and fetch outcome models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scrapeyard.models.job import ErrorType


@dataclass
class TargetResult:
    """Result of scraping a single target URL."""

    url: str
    status: str = "failed"
    data: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_scraped: int = 0
    error_type: ErrorType | None = None
    http_status: int | None = None
    error_detail: str | None = None
    debug: dict[str, Any] | None = None


@dataclass
class FetchOutcome:
    page: Any
    debug: dict[str, Any]


class FetchError(Exception):
    """Non-retryable HTTP error."""

    def __init__(self, status: int, debug: dict[str, Any] | None = None) -> None:
        self.status = status
        self.debug = debug
        super().__init__(f"HTTP {status}")
