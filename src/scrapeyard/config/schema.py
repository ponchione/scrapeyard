"""Pydantic models for the YAML configuration schema (spec section 3.5)."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field, HttpUrl, model_validator


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


class GroupBy(str, Enum):
    """Result grouping strategies."""

    target = "target"
    merge = "merge"


class FailStrategy(str, Enum):
    """How to handle target failures within a job."""

    partial = "partial"
    all_or_nothing = "all_or_nothing"
    continue_ = "continue"


class WebhookStatus(str, Enum):
    """Job statuses that can trigger a webhook."""

    complete = "complete"
    partial = "partial"
    failed = "failed"


class PricingVisibility(str, Enum):
    """Canonical pricing visibility states (Doc 1 Section 12.1)."""

    explicit = "explicit"
    map = "map"
    cart_only = "cart_only"
    call_for_price = "call_for_price"
    missing = "missing"
    unknown = "unknown"


class StockStatus(str, Enum):
    """Canonical stock status values (Doc 1 Section 12.2)."""

    in_stock = "in_stock"
    limited_stock = "limited_stock"
    out_of_stock = "out_of_stock"
    backorder = "backorder"
    preorder = "preorder"
    unknown = "unknown"


# --- Detection Config Models ---


class MapDetectionConfig(BaseModel):
    """MAP pricing detection patterns for a target (Doc 2 Section 2.2)."""

    text_patterns: list[str] = Field(
        default_factory=list,
        description="Text strings to match case-insensitively in item content",
    )
    css_selectors: list[str] = Field(
        default_factory=list,
        description="CSS selectors whose presence indicates MAP pricing",
    )
    price_value_patterns: list[str] = Field(
        default_factory=list,
        description="Raw price field values that indicate MAP (e.g. '<hidden-price>', '[price hidden]')",
    )


class StockPatternConfig(BaseModel):
    """Pattern set for a single stock status value."""

    text_patterns: list[str] = Field(
        default_factory=list,
        description="Text strings to match case-insensitively in item content",
    )
    css_selectors: list[str] = Field(
        default_factory=list,
        description="CSS selectors whose presence indicates this stock state",
    )


class StockDetectionConfig(BaseModel):
    """Stock status detection patterns, keyed by status value (Doc 1 Section 12.2)."""

    in_stock: Optional[StockPatternConfig] = None
    out_of_stock: Optional[StockPatternConfig] = None
    limited_stock: Optional[StockPatternConfig] = None
    backorder: Optional[StockPatternConfig] = None
    preorder: Optional[StockPatternConfig] = None


# --- Selector Models ---


class SelectorLong(BaseModel):
    """Long-form selector with explicit type and optional transform."""

    query: str
    type: SelectorType = SelectorType.css
    transform: Optional[str] = None


# A selector value is either a short-form string or a long-form object.
SelectorValue = Union[str, SelectorLong]


# --- Sub-config Models ---


class ProxyConfig(BaseModel):
    """Proxy configuration for a target, job, or service default."""

    url: str = Field(
        ...,
        description=(
            'Proxy gateway URL (e.g., "http://user:pass@gate.provider.com:7777") '
            'or "direct" to bypass proxying even when a default is set'
        ),
    )


class PaginationConfig(BaseModel):
    """Pagination rules for a target."""

    next: str = Field(..., description="CSS/XPath selector for the next-page element")
    max_pages: int = Field(default=10, description="Maximum pages to scrape")


class BrowserConfig(BaseModel):
    """Browser-backed fetcher tuning."""

    timeout_ms: int = Field(default=60000, description="Browser fetch timeout in milliseconds")
    disable_resources: bool = Field(
        default=True,
        description="Whether to block non-essential resources during browser fetches",
    )
    network_idle: bool = Field(
        default=False,
        description="Whether browser fetches should wait for network idle",
    )


class TargetConfig(BaseModel):
    """Single scrape target definition."""

    url: str = Field(..., description="Target URL to scrape")
    fetcher: FetcherType = Field(default=FetcherType.basic, description="Fetcher type")
    adaptive_domain: Optional[str] = Field(
        default=None,
        description="Optional adaptive fingerprint namespace override for this target",
    )
    browser: Optional[BrowserConfig] = Field(
        default=None,
        description="Optional browser-backed fetch tuning for stealthy and dynamic fetchers",
    )
    item_selector: Optional[SelectorValue] = Field(
        default=None,
        description="Optional selector for repeated item containers; when set, field selectors are applied relative to each matched item",
    )
    selectors: dict[str, SelectorValue] = Field(..., description="Named selector definitions")
    pagination: Optional[PaginationConfig] = None
    proxy: Optional[ProxyConfig] = Field(
        default=None,
        description="Target-level proxy override. Takes precedence over job and service defaults.",
    )
    map_detection: Optional[MapDetectionConfig] = Field(
        default=None,
        description="MAP pricing detection patterns. When present, enables pricing visibility classification.",
    )
    stock_detection: Optional[StockDetectionConfig] = Field(
        default=None,
        description="Stock status detection patterns, keyed by status value.",
    )


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
    fail_strategy: FailStrategy = Field(
        default=FailStrategy.partial, description="How to handle target failures"
    )


class ScheduleConfig(BaseModel):
    """Cron-style scheduling configuration."""

    cron: str = Field(..., description="Cron expression")
    enabled: bool = Field(default=True, description="Whether the schedule is active")


class OutputConfig(BaseModel):
    """Output grouping settings."""

    group_by: GroupBy = Field(default=GroupBy.target, description="Result grouping strategy")


class WebhookConfig(BaseModel):
    """Webhook notification configuration."""

    url: HttpUrl = Field(..., description="URL to POST webhook payload to")
    on: list[WebhookStatus] = Field(
        default=[WebhookStatus.complete, WebhookStatus.partial],
        description="Job statuses that trigger the webhook",
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="Custom HTTP headers"
    )
    timeout: int = Field(default=10, description="Timeout in seconds")


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
    proxy: Optional[ProxyConfig] = Field(
        default=None,
        description="Job-level proxy. Applies to all targets unless overridden at target level.",
    )

    retry: RetryConfig = Field(default_factory=RetryConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    schedule: Optional[ScheduleConfig] = None
    webhook: Optional[WebhookConfig] = None
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _check_target_mutual_exclusivity(self) -> ScrapeConfig:
        has_target = self.target is not None
        has_targets = self.targets is not None
        if has_target and has_targets:
            raise ValueError("Specify either 'target' or 'targets', not both")
        if not has_target and not has_targets:
            raise ValueError("One of 'target' or 'targets' must be provided")
        return self

    def resolved_targets(self) -> list[TargetConfig]:
        """Return the list of targets regardless of Tier 1 or Tier 2 config."""
        if self.target is not None:
            return [self.target]
        return self.targets  # type: ignore[return-value]
