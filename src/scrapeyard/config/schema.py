"""Pydantic models for the YAML configuration schema (spec section 3.5)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from scrapeyard.common.paths import safe_path_part
from scrapeyard.config.transforms import parse_transform
from scrapeyard.engine.proxy import normalize_public_proxy_url
from scrapeyard.engine.url_guard import UnsafeURLError, assert_public_url

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_CUSTOM_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "expect",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_FORBIDDEN_BROWSER_ADDITIONAL_ARGUMENTS = frozenset({"addons", "proxy"})

MAX_BROWSER_ACTIONS = 50
MAX_BROWSER_ACTION_REPEAT = 50
MAX_BROWSER_TIMEOUT_MS = 300_000
MAX_BROWSER_WAIT_MS = 60_000
MAX_DOMAIN_RATE_LIMIT_SECONDS = 3_600
MAX_EXECUTION_CONCURRENCY = 50
MAX_EXECUTION_DELAY_SECONDS = 3_600
MAX_PAGINATION_PAGES = 100
MAX_RETRY_ATTEMPTS = 10
MAX_RETRY_BACKOFF_SECONDS = 300
MAX_RETRYABLE_STATUSES = 32
MAX_TARGETS_PER_JOB = 100
MAX_WEBHOOK_TIMEOUT_SECONDS = 60


# --- Enums ---


class FetcherType(str, Enum):
    """Supported fetcher types."""

    basic = "basic"
    stealthy = "stealthy"
    dynamic = "dynamic"


class BrowserActionType(str, Enum):
    """Supported browser page actions before extraction."""

    click = "click"
    wait_for_selector = "wait_for_selector"
    wait_ms = "wait_ms"
    scroll = "scroll"
    repeat_click = "repeat_click"


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


class StrictConfigModel(BaseModel):
    """Base for user-facing YAML config models; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class MapDetectionConfig(StrictConfigModel):
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


class StockPatternConfig(StrictConfigModel):
    """Pattern set for a single stock status value."""

    text_patterns: list[str] = Field(
        default_factory=list,
        description="Text strings to match case-insensitively in item content",
    )
    css_selectors: list[str] = Field(
        default_factory=list,
        description="CSS selectors whose presence indicates this stock state",
    )


class StockDetectionConfig(StrictConfigModel):
    """Stock status detection patterns, keyed by status value (Doc 1 Section 12.2)."""

    in_stock: Optional[StockPatternConfig] = None
    out_of_stock: Optional[StockPatternConfig] = None
    limited_stock: Optional[StockPatternConfig] = None
    backorder: Optional[StockPatternConfig] = None
    preorder: Optional[StockPatternConfig] = None


# --- Selector Models ---


class SelectorLong(StrictConfigModel):
    """Long-form selector with explicit type and optional transform."""

    query: str
    type: SelectorType = SelectorType.css
    transform: Optional[str] = None

    @field_validator("transform")
    @classmethod
    def _validate_transform_pipeline(cls, value: str | None) -> str | None:
        if value is None:
            return value
        for step in value.split("|"):
            step = step.strip()
            if not step:
                raise ValueError("Selector transform steps must not be blank")
            try:
                parse_transform(step)
            except (ValueError, re.error) as exc:
                raise ValueError(f"Invalid selector transform: {exc}") from exc
        return value


# A selector value is either a short-form string or a long-form object.
SelectorValue = Union[str, SelectorLong]


def _validate_http_headers(headers: dict[str, str]) -> dict[str, str]:
    for name, value in headers.items():
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid HTTP header name: {name!r}")
        if name.lower() in _FORBIDDEN_CUSTOM_HEADERS:
            raise ValueError(f"HTTP header {name!r} is managed by the HTTP client")
        _validate_header_value(value, label=f"HTTP header {name!r}")
    return headers


def _validate_header_value(value: str, *, label: str) -> str:
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} must not contain CR, LF, or NUL")
    return value


# --- Sub-config Models ---


class ProxyConfig(StrictConfigModel):
    """Proxy configuration for a target, job, or service default."""

    url: str = Field(
        ...,
        description=(
            'Proxy gateway URL (e.g., "http://user:pass@gate.provider.com:7777") '
            'or "direct" to bypass proxying even when a default is set'
        ),
    )

    @field_validator("url")
    @classmethod
    def _validate_proxy_url(cls, value: str) -> str:
        return normalize_public_proxy_url(value)


class PaginationConfig(StrictConfigModel):
    """Pagination rules for a target."""

    next: SelectorValue = Field(..., description="CSS/XPath selector for the next-page element")
    max_pages: int = Field(
        default=10,
        ge=0,
        le=MAX_PAGINATION_PAGES,
        description="Maximum pages to scrape",
    )


class BrowserActionConfig(StrictConfigModel):
    """One browser action to run after page load and before extraction."""

    type: BrowserActionType
    selector: str | None = Field(default=None, description="CSS selector used by click/wait actions")
    optional: bool = Field(
        default=False,
        description="Continue when this action cannot be completed",
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=1,
        le=MAX_BROWSER_TIMEOUT_MS,
        description="Optional timeout in milliseconds for selector-based actions",
    )
    wait_ms: int | None = Field(
        default=None,
        ge=0,
        le=MAX_BROWSER_WAIT_MS,
        description="Optional wait in milliseconds after this action",
    )
    times: int = Field(
        default=1,
        ge=1,
        le=MAX_BROWSER_ACTION_REPEAT,
        description="Number of scroll iterations",
    )
    pixels: int = Field(default=1200, description="Vertical pixels per scroll action")
    max_times: int = Field(
        default=1,
        ge=1,
        le=MAX_BROWSER_ACTION_REPEAT,
        description="Maximum repeat_click attempts",
    )
    wait_for_selector: str | None = Field(
        default=None,
        description="Optional CSS selector to wait for after each repeat_click",
    )

    @field_validator("pixels")
    @classmethod
    def _pixels_must_be_nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("pixels must be non-zero")
        return value

    @model_validator(mode="after")
    def _validate_action_requirements(self) -> BrowserActionConfig:
        if self.type in {
            BrowserActionType.click,
            BrowserActionType.wait_for_selector,
            BrowserActionType.repeat_click,
        } and not self.selector:
            raise ValueError(f"{self.type.value} action requires 'selector'")
        if self.type == BrowserActionType.wait_ms and self.wait_ms is None:
            raise ValueError("wait_ms action requires 'wait_ms'")
        return self


class BrowserConfig(StrictConfigModel):
    """Browser-backed fetcher tuning."""

    timeout_ms: int = Field(
        default=60000,
        gt=0,
        le=MAX_BROWSER_TIMEOUT_MS,
        description="Browser fetch timeout in milliseconds",
    )
    disable_resources: bool = Field(
        default=True,
        description="Whether to block non-essential resources during browser fetches",
    )
    network_idle: bool = Field(
        default=False,
        description="Whether browser fetches should wait for network idle",
    )
    stealth: bool = Field(
        default=False,
        description="Enable stealth mode to reduce bot detection (Playwright anti-fingerprinting)",
    )
    hide_canvas: bool = Field(
        default=False,
        description="Mask HTML canvas fingerprinting when stealth is enabled",
    )
    real_chrome: bool = Field(
        default=False,
        description="Launch a real Chrome channel instead of bundled Chromium when supported by the dynamic fetcher",
    )
    cdp_url: str | None = Field(
        default=None,
        description="Optional Chrome DevTools Protocol endpoint for attaching the dynamic fetcher to an existing browser",
    )
    nstbrowser_mode: bool = Field(
        default=False,
        description="Enable NSTBrowser integration mode for the dynamic fetcher when supported upstream",
    )
    humanize: bool | float | None = Field(
        default=None,
        description="Optional humanization delay/behavior override for the stealthy fetcher",
    )
    os_randomize: bool = Field(
        default=False,
        description="Randomize reported operating-system traits when supported by the stealthy fetcher",
    )
    geoip: bool = Field(
        default=False,
        description="Align stealthy browser geography signals with proxy geography when supported upstream",
    )
    disable_ads: bool = Field(
        default=False,
        description="Enable ad-blocking behavior for stealthy fetches when supported upstream",
    )
    additional_arguments: dict[str, object] = Field(
        default_factory=dict,
        description="Extra upstream stealthy/Camoufox arguments for narrowly scoped hostile-site probes",
    )
    useragent: str | None = Field(
        default=None,
        description="Custom User-Agent string override for browser fetches",
    )
    extra_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Additional HTTP headers injected into every browser request",
    )
    click_selector: str | None = Field(
        default=None,
        description="Optional CSS selector to click before extracting data (for consent/age gates)",
    )
    click_timeout_ms: int | None = Field(
        default=3000,
        ge=1,
        le=MAX_BROWSER_TIMEOUT_MS,
        description="Optional timeout in milliseconds for click_selector before falling through",
    )
    click_wait_ms: int | None = Field(
        default=None,
        ge=0,
        le=MAX_BROWSER_WAIT_MS,
        description="Optional extra browser wait in milliseconds after click_selector is clicked",
    )
    wait_for_selector: str | None = Field(
        default=None,
        description="Optional CSS selector to wait for before extracting data",
    )
    wait_ms: int | None = Field(
        default=None,
        ge=0,
        le=MAX_BROWSER_WAIT_MS,
        description="Optional extra browser wait in milliseconds after page load/selector wait",
    )
    actions: list[BrowserActionConfig] = Field(
        default_factory=list,
        max_length=MAX_BROWSER_ACTIONS,
        description="Ordered browser actions to run after page load and before extraction",
    )

    @field_validator("useragent")
    @classmethod
    def _reject_invalid_useragent(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_header_value(value, label="useragent")

    @field_validator("extra_headers")
    @classmethod
    def _reject_invalid_extra_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_http_headers(value)

    @field_validator("additional_arguments")
    @classmethod
    def _reject_managed_additional_arguments(cls, value: dict[str, object]) -> dict[str, object]:
        forbidden = sorted(
            key for key in value if key.lower() in _FORBIDDEN_BROWSER_ADDITIONAL_ARGUMENTS
        )
        if forbidden:
            raise ValueError(
                "browser.additional_arguments must not override managed option(s): "
                + ", ".join(forbidden)
            )
        return value

    @field_validator("cdp_url")
    @classmethod
    def _reject_unsafe_cdp_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            assert_public_url(value, allowed_schemes=("http", "https", "ws", "wss"))
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc
        return value


@dataclass(frozen=True)
class BrowserFetchKwarg:
    """Mapping from BrowserConfig field to upstream Scrapling fetch kwarg."""

    field_name: str
    kwarg_name: str
    send_when: str = "truthy"

    def should_send(self, value: object) -> bool:
        if self.send_when == "always":
            return True
        if self.send_when == "not_none":
            return value is not None
        return bool(value)


BROWSER_FETCH_KWARGS: tuple[BrowserFetchKwarg, ...] = (
    BrowserFetchKwarg("timeout_ms", "timeout", "always"),
    BrowserFetchKwarg("disable_resources", "disable_resources", "always"),
    BrowserFetchKwarg("network_idle", "network_idle", "always"),
    BrowserFetchKwarg("stealth", "stealth", "always"),
    BrowserFetchKwarg("hide_canvas", "hide_canvas", "always"),
    BrowserFetchKwarg("real_chrome", "real_chrome", "always"),
    BrowserFetchKwarg("nstbrowser_mode", "nstbrowser_mode", "always"),
    BrowserFetchKwarg("useragent", "useragent"),
    BrowserFetchKwarg("extra_headers", "extra_headers"),
    BrowserFetchKwarg("cdp_url", "cdp_url"),
    BrowserFetchKwarg("humanize", "humanize", "not_none"),
    BrowserFetchKwarg("os_randomize", "os_randomize"),
    BrowserFetchKwarg("geoip", "geoip"),
    BrowserFetchKwarg("disable_ads", "disable_ads"),
    BrowserFetchKwarg("additional_arguments", "additional_arguments"),
    BrowserFetchKwarg("wait_for_selector", "wait_selector"),
    BrowserFetchKwarg("wait_ms", "wait", "not_none"),
)


class TargetConfig(StrictConfigModel):
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
    selectors: dict[str, SelectorValue] = Field(
        ...,
        min_length=1,
        description="Named selector definitions",
    )
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

    @field_validator("url")
    @classmethod
    def _reject_unsafe_url(cls, value: str) -> str:
        try:
            assert_public_url(value)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc
        return value


class RetryConfig(StrictConfigModel):
    """Retry policy configuration."""

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=MAX_RETRY_ATTEMPTS,
        description="Maximum retry attempts per request",
    )
    backoff: BackoffStrategy = Field(
        default=BackoffStrategy.exponential, description="Backoff strategy"
    )
    backoff_max: int = Field(
        default=30,
        ge=0,
        le=MAX_RETRY_BACKOFF_SECONDS,
        description="Maximum backoff delay in seconds",
    )
    retryable_status: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504],
        max_length=MAX_RETRYABLE_STATUSES,
        description="HTTP status codes that trigger a retry",
    )

    @field_validator("retryable_status")
    @classmethod
    def _validate_retryable_status(cls, value: list[int]) -> list[int]:
        invalid = [status for status in value if status < 100 or status > 599]
        if invalid:
            raise ValueError("retryable_status values must be valid HTTP status codes")
        return value


class ValidationConfig(StrictConfigModel):
    """Result validation rules."""

    required_fields: list[str] = Field(
        default_factory=list, description="Fields that must be non-empty"
    )
    min_results: int = Field(default=0, ge=0, description="Minimum number of results expected")
    on_empty: OnEmptyAction = Field(
        default=OnEmptyAction.warn, description="Action when selectors return empty"
    )


class ExecutionConfig(StrictConfigModel):
    """Concurrency and orchestration settings."""

    concurrency: int = Field(
        default=2,
        ge=1,
        le=MAX_EXECUTION_CONCURRENCY,
        description="Max simultaneous targets within this job",
    )
    delay_between: int = Field(
        default=2,
        ge=0,
        le=MAX_EXECUTION_DELAY_SECONDS,
        description="Seconds between starting concurrent targets",
    )
    domain_rate_limit: int = Field(
        default=3,
        ge=0,
        le=MAX_DOMAIN_RATE_LIMIT_SECONDS,
        description="Minimum seconds between requests to same domain",
    )
    mode: ExecutionMode = Field(default=ExecutionMode.auto, description="Response mode")
    priority: Priority = Field(default=Priority.normal, description="Queue priority")
    fail_strategy: FailStrategy = Field(
        default=FailStrategy.partial, description="How to handle target failures"
    )


class ScheduleConfig(StrictConfigModel):
    """Cron-style scheduling configuration."""

    cron: str = Field(..., description="Cron expression")
    enabled: bool = Field(default=True, description="Whether the schedule is active")

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
        return value


class OutputConfig(StrictConfigModel):
    """Output grouping settings."""

    group_by: GroupBy = Field(default=GroupBy.target, description="Result grouping strategy")


class WebhookConfig(StrictConfigModel):
    """Webhook notification configuration."""

    url: HttpUrl = Field(..., description="URL to POST webhook payload to")
    on: list[WebhookStatus] = Field(
        default_factory=lambda: [WebhookStatus.complete, WebhookStatus.partial],
        description="Job statuses that trigger the webhook",
    )
    headers: dict[str, str] = Field(
        default_factory=dict, description="Custom HTTP headers"
    )
    timeout: int = Field(
        default=10,
        gt=0,
        le=MAX_WEBHOOK_TIMEOUT_SECONDS,
        description="Timeout in seconds",
    )

    @field_validator("headers")
    @classmethod
    def _reject_invalid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_http_headers(value)

    @field_validator("url")
    @classmethod
    def _reject_unsafe_url(cls, value: HttpUrl) -> HttpUrl:
        try:
            assert_public_url(str(value))
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc
        return value


# --- Top-Level Config ---


class ScrapeConfig(StrictConfigModel):
    """Top-level YAML configuration schema (spec section 3.5).

    Supports both Tier 1 (single target) and Tier 2 (multi-target) configs.
    """

    project: str = Field(..., description="Project namespace")
    name: str = Field(..., description="Unique job name within the project")

    # Single target (Tier 1) or multiple targets (Tier 2) — one must be provided.
    target: Optional[TargetConfig] = None
    targets: Optional[list[TargetConfig]] = Field(
        default=None,
        min_length=1,
        max_length=MAX_TARGETS_PER_JOB,
    )

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

    @field_validator("project", "name")
    @classmethod
    def _reject_unsafe_storage_component(cls, value: str) -> str:
        return safe_path_part(value, label="project/name")

    @model_validator(mode="after")
    def _normalize_targets(self) -> ScrapeConfig:
        has_target = self.target is not None
        has_targets = self.targets is not None
        if has_target and has_targets:
            raise ValueError("Specify either 'target' or 'targets', not both")
        if not has_target and not has_targets:
            raise ValueError("One of 'target' or 'targets' must be provided")
        if self.targets is None and self.target is not None:
            self.targets = [self.target]
        return self

    def resolved_targets(self) -> list[TargetConfig]:
        """Return the list of targets regardless of Tier 1 or Tier 2 config."""
        if self.targets is None:
            raise RuntimeError("ScrapeConfig targets were not normalized")
        return self.targets
