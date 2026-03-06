"""Pydantic models for the YAML configuration schema (spec section 3.5).

Validators and transform parsing are deferred to a later work order.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field


# --- Enums ---


class FetcherType(str, Enum):
    """Supported fetcher types."""

    basic = "basic"
    stealthy = "stealthy"
    dynamic = "dynamic"


class SelectorType(str, Enum):
    """Supported selector query types."""

    css = "css"
    xpath = "xpath"


class BackoffStrategy(str, Enum):
    """Retry backoff strategies."""

    exponential = "exponential"
    linear = "linear"
    fixed = "fixed"


class OnEmptyAction(str, Enum):
    """Actions when selectors return empty results."""

    retry = "retry"
    warn = "warn"
    fail = "fail"
    skip = "skip"


class ExecutionMode(str, Enum):
    """Response mode for the API."""

    auto = "auto"
    sync = "sync"
    async_ = "async"


class Priority(str, Enum):
    """Queue priority levels."""

    high = "high"
    normal = "normal"
    low = "low"


class OutputFormat(str, Enum):
    """Supported output formats."""

    json = "json"
    markdown = "markdown"
    html = "html"
    json_markdown = "json+markdown"


class GroupBy(str, Enum):
    """Result grouping strategies."""

    target = "target"
    merge = "merge"


# --- Selector Models ---


class SelectorLong(BaseModel):
    """Long-form selector with explicit type and optional transform."""

    query: str
    type: SelectorType = SelectorType.css
    transform: Optional[str] = None


# A selector value is either a short-form string or a long-form object.
SelectorValue = Union[str, SelectorLong]


# --- Sub-config Models ---


class PaginationConfig(BaseModel):
    """Pagination rules for a target."""

    next: str = Field(..., description="CSS/XPath selector for the next-page element")
    max_pages: int = Field(default=10, description="Maximum pages to scrape")


class TargetConfig(BaseModel):
    """Single scrape target definition."""

    url: str = Field(..., description="Target URL to scrape")
    fetcher: FetcherType = Field(default=FetcherType.basic, description="Fetcher type")
    selectors: dict[str, SelectorValue] = Field(..., description="Named selector definitions")
    pagination: Optional[PaginationConfig] = None


class RetryConfig(BaseModel):
    """Retry policy configuration."""

    max_attempts: int = Field(default=3, description="Maximum retry attempts per request")
    backoff: BackoffStrategy = Field(
        default=BackoffStrategy.exponential, description="Backoff strategy"
    )
    backoff_max: int = Field(default=30, description="Maximum backoff delay in seconds")
    retryable_status: list[int] = Field(
        default=[429, 500, 502, 503, 504],
        description="HTTP status codes that trigger a retry",
    )


class ValidationConfig(BaseModel):
    """Result validation rules."""

    required_fields: list[str] = Field(
        default_factory=list, description="Fields that must be non-empty"
    )
    min_results: int = Field(default=0, description="Minimum number of results expected")
    on_empty: OnEmptyAction = Field(
        default=OnEmptyAction.warn, description="Action when selectors return empty"
    )


class ExecutionConfig(BaseModel):
    """Concurrency and orchestration settings."""

    concurrency: int = Field(default=2, description="Max simultaneous targets within this job")
    delay_between: int = Field(default=2, description="Seconds between starting concurrent targets")
    domain_rate_limit: int = Field(
        default=3, description="Minimum seconds between requests to same domain"
    )
    mode: ExecutionMode = Field(default=ExecutionMode.auto, description="Response mode")
    priority: Priority = Field(default=Priority.normal, description="Queue priority")


class ScheduleConfig(BaseModel):
    """Cron-style scheduling configuration."""

    cron: str = Field(..., description="Cron expression")
    enabled: bool = Field(default=True, description="Whether the schedule is active")


class OutputConfig(BaseModel):
    """Output format and grouping settings."""

    format: OutputFormat = Field(default=OutputFormat.json, description="Output format")
    group_by: GroupBy = Field(default=GroupBy.target, description="Result grouping strategy")


# --- Top-Level Config ---


class ScrapeConfig(BaseModel):
    """Top-level YAML configuration schema (spec section 3.5).

    Supports both Tier 1 (single target) and Tier 2 (multi-target) configs.
    """

    project: str = Field(..., description="Project namespace")
    name: str = Field(..., description="Unique job name within the project")

    # Single target (Tier 1) or multiple targets (Tier 2) — one must be provided.
    target: Optional[TargetConfig] = None
    targets: Optional[list[TargetConfig]] = None

    adaptive: Optional[bool] = Field(
        default=None, description="Override adaptive tracking (default: auto)"
    )

    retry: RetryConfig = Field(default_factory=RetryConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    schedule: Optional[ScheduleConfig] = None
    output: OutputConfig = Field(default_factory=OutputConfig)
