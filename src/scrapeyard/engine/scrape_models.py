"""Shared scraper result and fetch outcome models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from scrapeyard.models.job import ErrorType


class TargetStatus(str, Enum):
    """Possible states of a single target scrape attempt."""

    success = "success"
    failed = "failed"


@dataclass
class TargetResult:
    """Result of scraping a single target URL."""

    url: str
    status: TargetStatus | str = TargetStatus.failed
    data: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_scraped: int = 0
    error_type: ErrorType | None = None
    http_status: int | None = None
    error_detail: str | None = None
    debug: dict[str, Any] | None = None
    pagination_stop_reason: Literal[
        "unknown", "not_configured", "exhausted", "max_pages",
        "repeated_url", "repeated_page", "unsafe_next_url", "invalid_next_link",
        "domain_guard", "cache_miss",
    ] = "unknown"
    # Earliest recording time of pages served from the page cache (replay only).
    recorded_at: str | None = None

    def __post_init__(self) -> None:
        self.status = TargetStatus(self.status)

    @property
    def is_success(self) -> bool:
        return self.status is TargetStatus.success

    @property
    def is_failed(self) -> bool:
        return self.status is TargetStatus.failed

    @property
    def status_value(self) -> str:
        return TargetStatus(self.status).value


@dataclass
class FetchOutcome:
    page: object
    debug: dict[str, Any]


@dataclass(frozen=True)
class ClickPaginationSpec:
    """Click-pagination instructions for one live browser fetch."""

    next_query: str
    next_type: str
    item_query: str | None
    item_type: str | None
    max_pages: int


@dataclass(frozen=True)
class ClickPaginationResult:
    """Rendered follow-on pages captured by clicking the next element."""

    pages: list[object] = field(default_factory=list)
    stop_reason: str = "unknown"
    stop_detail: str | None = None


# The live browser fetch attaches click-pagination snapshots to the first page.
CLICK_PAGINATION_ATTRIBUTE = "scrapeyard_click_pagination"


class ScrapeStop(Exception):
    """Scrapeyard stopped itself before making a request (not a site failure).

    Subclasses set ``error_type`` and the pagination stop reason used when the
    stop happens after the first page.
    """

    error_type: ErrorType
    pagination_stop_reason: str = "unknown"


# Error types for stops Scrapeyard imposes on itself; they are not upstream
# failures and never count against a domain's circuit breaker.
SELF_IMPOSED_ERROR_TYPES = frozenset({
    ErrorType.cache_miss,
    ErrorType.domain_daily_limit,
    ErrorType.domain_cooldown,
})


class FetchError(Exception):
    """Non-retryable HTTP error."""

    def __init__(self, status: int, debug: dict[str, Any] | None = None) -> None:
        self.status = status
        self.debug = debug
        super().__init__(f"HTTP {status}")
