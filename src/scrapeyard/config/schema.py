"""Pydantic models for the YAML configuration schema (spec section 3.5)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from scrapeyard.common.paths import safe_path_part
from scrapeyard.config.transforms import parse_transform_pipeline
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
_ALLOWED_BROWSER_ADDITIONAL_ARGUMENTS = frozenset(
    {
        "custom_fonts_only",
        "fonts",
        "locale",
        "window",
    }
)
_BROWSER_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MAX_BROWSER_FONTS = 64
_MAX_BROWSER_FONT_NAME_CHARS = 128
_MAX_BROWSER_LOCALES = 16
_MAX_BROWSER_LOCALE_CHARS = 64
_MAX_BROWSER_WINDOW_DIMENSION = 16_384

MAX_BROWSER_ACTIONS = 50
MAX_BROWSER_ACTION_REPEAT = 50
MAX_BROWSER_HUMANIZE_SECONDS = 60.0
MAX_BROWSER_TIMEOUT_MS = 300_000
MAX_BROWSER_WAIT_MS = 60_000
MAX_DETECTION_CSS_SELECTORS = 50
MAX_DETECTION_PATTERNS = 100
MAX_DETECTION_TEXT_PATTERN_CHARS = 512
MAX_DOMAIN_RATE_LIMIT_SECONDS = 3_600
MAX_EXECUTION_CONCURRENCY = 50
MAX_EXECUTION_DELAY_SECONDS = 3_600
MAX_PAGINATION_PAGES = 100
MAX_RETRY_ATTEMPTS = 10
MAX_RETRY_BACKOFF_SECONDS = 300
MAX_RETRYABLE_STATUSES = 32
MAX_REQUIRED_FIELDS = 100
MAX_SELECTORS_PER_TARGET = 100
MAX_SELECTOR_FIELD_NAME_CHARS = 256
MAX_SELECTOR_QUERY_CHARS = 4096
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

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def _reject_boolean_numeric_values(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        """Keep YAML booleans from silently becoming numeric controls."""

        def contains(annotation: object, expected: type[object]) -> bool:
            if annotation is expected:
                return True
            return any(contains(arg, expected) for arg in get_args(annotation))

        if info.field_name is None:
            return value
        field = cls.model_fields[info.field_name]
        annotation = field.annotation
        accepts_bool = contains(annotation, bool)
        numeric = contains(annotation, int) or contains(annotation, float)
        if isinstance(value, bool) and numeric and not accepts_bool:
            raise ValueError("numeric configuration values must not be booleans")

        origin = get_origin(annotation)
        args = get_args(annotation)
        if (
            origin is list
            and args
            and contains(args[0], int)
            and isinstance(value, list)
            and any(isinstance(item, bool) for item in value)
        ):
            raise ValueError("numeric configuration values must not be booleans")
        return value


def _validate_pattern_list(
    values: list[str],
    *,
    label: str,
    max_item_chars: int,
    case_insensitive: bool,
    allow_explicit_empty: bool = False,
) -> list[str]:
    """Normalize bounded pattern lists and reject ambiguous duplicates."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if allow_explicit_empty and value == "":
            if value in seen:
                raise ValueError(f"{label} values must be unique")
            seen.add(value)
            normalized.append(value)
            continue
        item = value.strip()
        if not item:
            raise ValueError(f"{label} values must not be blank")
        if len(item) > max_item_chars:
            raise ValueError(
                f"{label} values must not exceed {max_item_chars} characters"
            )
        identity = item.casefold() if case_insensitive else item
        if identity in seen:
            raise ValueError(f"{label} values must be unique")
        seen.add(identity)
        normalized.append(item.lower() if case_insensitive else item)
    return normalized


class MapDetectionConfig(StrictConfigModel):
    """MAP pricing detection patterns for a target (Doc 2 Section 2.2)."""

    text_patterns: list[str] = Field(
        default_factory=list,
        max_length=MAX_DETECTION_PATTERNS,
        description="Text strings to match case-insensitively in item content",
    )
    css_selectors: list[str] = Field(
        default_factory=list,
        max_length=MAX_DETECTION_CSS_SELECTORS,
        description="CSS selectors whose presence indicates MAP pricing",
    )
    price_value_patterns: list[str] = Field(
        default_factory=list,
        max_length=MAX_DETECTION_PATTERNS,
        description="Raw price field values that indicate MAP (e.g. '<hidden-price>', '[price hidden]')",
    )

    @field_validator("text_patterns")
    @classmethod
    def _validate_text_patterns(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="MAP text_patterns",
            max_item_chars=MAX_DETECTION_TEXT_PATTERN_CHARS,
            case_insensitive=True,
        )

    @field_validator("css_selectors")
    @classmethod
    def _validate_css_selectors(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="MAP css_selectors",
            max_item_chars=MAX_SELECTOR_QUERY_CHARS,
            case_insensitive=False,
        )

    @field_validator("price_value_patterns")
    @classmethod
    def _validate_price_value_patterns(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="MAP price_value_patterns",
            max_item_chars=MAX_DETECTION_TEXT_PATTERN_CHARS,
            case_insensitive=False,
            allow_explicit_empty=True,
        )


class StockPatternConfig(StrictConfigModel):
    """Pattern set for a single stock status value."""

    text_patterns: list[str] = Field(
        default_factory=list,
        max_length=MAX_DETECTION_PATTERNS,
        description="Text strings to match case-insensitively in item content",
    )
    css_selectors: list[str] = Field(
        default_factory=list,
        max_length=MAX_DETECTION_CSS_SELECTORS,
        description="CSS selectors whose presence indicates this stock state",
    )

    @field_validator("text_patterns")
    @classmethod
    def _validate_text_patterns(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="Stock text_patterns",
            max_item_chars=MAX_DETECTION_TEXT_PATTERN_CHARS,
            case_insensitive=True,
        )

    @field_validator("css_selectors")
    @classmethod
    def _validate_css_selectors(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="Stock css_selectors",
            max_item_chars=MAX_SELECTOR_QUERY_CHARS,
            case_insensitive=False,
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

    query: str = Field(min_length=1, max_length=MAX_SELECTOR_QUERY_CHARS)
    type: SelectorType = SelectorType.css
    transform: Optional[str] = None

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Selector queries must not be blank")
        return value

    @field_validator("transform")
    @classmethod
    def _validate_transform_pipeline(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parse_transform_pipeline(value)
        except (ValueError, re.error) as exc:
            raise ValueError(f"Invalid selector transform: {exc}") from exc
        return value


# A selector value is either a short-form string or a long-form object.
SelectorValue = Union[str, SelectorLong]


def _validate_short_selector_query(value: str) -> str:
    if not value.strip():
        raise ValueError("Selector queries must not be blank")
    if len(value) > MAX_SELECTOR_QUERY_CHARS:
        raise ValueError(
            f"Selector queries must not exceed {MAX_SELECTOR_QUERY_CHARS} characters"
        )
    return value


def _validate_http_headers(headers: dict[str, str]) -> dict[str, str]:
    seen: set[str] = set()
    for name, value in headers.items():
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid HTTP header name: {name!r}")
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(f"Duplicate HTTP header name: {name!r}")
        seen.add(lowered)
        if lowered in _FORBIDDEN_CUSTOM_HEADERS:
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
        ge=1,
        le=MAX_PAGINATION_PAGES,
        description="Maximum pages to scrape",
    )

    @field_validator("next")
    @classmethod
    def _validate_next_selector(cls, value: SelectorValue) -> SelectorValue:
        if isinstance(value, str):
            _validate_short_selector_query(value)
        return value


class BrowserActionConfig(StrictConfigModel):
    """One browser action to run after page load and before extraction."""

    type: BrowserActionType
    selector: str | None = Field(
        default=None, description="CSS selector used by click/wait actions"
    )
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
        description="Optional CSS selector to wait for after click or repeat_click",
    )

    @field_validator("selector", "wait_for_selector")
    @classmethod
    def _validate_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_short_selector_query(value)

    @field_validator("pixels")
    @classmethod
    def _pixels_must_be_nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("pixels must be non-zero")
        return value

    @model_validator(mode="after")
    def _validate_action_requirements(self) -> BrowserActionConfig:
        if (
            self.type
            in {
                BrowserActionType.click,
                BrowserActionType.wait_for_selector,
                BrowserActionType.repeat_click,
            }
            and not self.selector
        ):
            raise ValueError(f"{self.type.value} action requires 'selector'")
        if self.type == BrowserActionType.wait_ms and self.wait_ms is None:
            raise ValueError("wait_ms action requires 'wait_ms'")

        allowed_fields = {
            BrowserActionType.click: {
                "type",
                "selector",
                "optional",
                "timeout_ms",
                "wait_ms",
                "wait_for_selector",
            },
            BrowserActionType.wait_for_selector: {
                "type",
                "selector",
                "optional",
                "timeout_ms",
                "wait_ms",
            },
            BrowserActionType.wait_ms: {"type", "optional", "wait_ms"},
            BrowserActionType.scroll: {
                "type",
                "optional",
                "wait_ms",
                "times",
                "pixels",
            },
            BrowserActionType.repeat_click: {
                "type",
                "selector",
                "optional",
                "timeout_ms",
                "wait_ms",
                "max_times",
                "wait_for_selector",
            },
        }
        unsupported = sorted(self.model_fields_set - allowed_fields[self.type])
        if unsupported:
            raise ValueError(
                f"{self.type.value} action does not use field(s): "
                + ", ".join(unsupported)
            )
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
        description=(
            "Optional stealthy-fetcher humanization behavior; a numeric value is "
            "the maximum cursor-movement duration in seconds and must be positive "
            f"and no greater than {MAX_BROWSER_HUMANIZE_SECONDS:g}"
        ),
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
        description=(
            "Additional HTTP headers added to one HTTP hop at a time only when "
            "scheme, hostname, and effective port match the exact target origin"
        ),
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

    @field_validator("humanize", mode="before")
    @classmethod
    def _validate_humanize(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if not isinstance(value, (str, int, float)):
            return value
        try:
            duration = float(value)
        except ValueError:
            return value
        except OverflowError:
            duration = math.inf
        if not math.isfinite(duration) or not 0 < duration <= MAX_BROWSER_HUMANIZE_SECONDS:
            raise ValueError(
                "browser.humanize numeric duration must be finite, positive, "
                f"and no greater than {MAX_BROWSER_HUMANIZE_SECONDS:g} seconds"
            )
        return value

    @field_validator("extra_headers")
    @classmethod
    def _reject_invalid_extra_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_http_headers(value)

    @field_validator("click_selector", "wait_for_selector")
    @classmethod
    def _validate_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_short_selector_query(value)

    @field_validator("additional_arguments")
    @classmethod
    def _validate_additional_arguments(
        cls,
        value: dict[str, object],
    ) -> dict[str, object]:
        unsupported = sorted(
            key for key in value if key not in _ALLOWED_BROWSER_ADDITIONAL_ARGUMENTS
        )
        if unsupported:
            raise ValueError(
                "browser.additional_arguments contains unsupported or unsafe option(s): "
                + ", ".join(unsupported)
            )

        locale = value.get("locale")
        if locale is not None:
            locales = locale if isinstance(locale, list) else [locale]
            if not locales or len(locales) > _MAX_BROWSER_LOCALES:
                raise ValueError(
                    "browser.additional_arguments.locale must contain between "
                    f"1 and {_MAX_BROWSER_LOCALES} locale tags"
                )
            if any(
                not isinstance(item, str)
                or len(item) > _MAX_BROWSER_LOCALE_CHARS
                or not _BROWSER_LOCALE_RE.fullmatch(item)
                for item in locales
            ):
                raise ValueError(
                    "browser.additional_arguments.locale must be a locale tag "
                    "or a list of locale tags"
                )

        fonts = value.get("fonts")
        if fonts is not None:
            if not isinstance(fonts, list) or len(fonts) > _MAX_BROWSER_FONTS:
                raise ValueError(
                    "browser.additional_arguments.fonts must be a list of at "
                    f"most {_MAX_BROWSER_FONTS} font names"
                )
            if any(
                not isinstance(font, str)
                or not font.strip()
                or len(font) > _MAX_BROWSER_FONT_NAME_CHARS
                or any(ord(char) < 32 for char in font)
                for font in fonts
            ):
                raise ValueError("browser.additional_arguments.fonts contains an invalid font name")

        custom_fonts_only = value.get("custom_fonts_only")
        if custom_fonts_only is not None and not isinstance(custom_fonts_only, bool):
            raise ValueError("browser.additional_arguments.custom_fonts_only must be a boolean")
        if custom_fonts_only is True and not fonts:
            raise ValueError(
                "browser.additional_arguments.custom_fonts_only requires at least one font"
            )

        window = value.get("window")
        if window is not None and (
            not isinstance(window, list)
            or len(window) != 2
            or any(
                isinstance(dimension, bool)
                or not isinstance(dimension, int)
                or not 1 <= dimension <= _MAX_BROWSER_WINDOW_DIMENSION
                for dimension in window
            )
        ):
            raise ValueError(
                "browser.additional_arguments.window must be a two-item "
                "list of positive integer dimensions no greater than "
                f"{_MAX_BROWSER_WINDOW_DIMENSION}"
            )
        return value

    @field_validator("cdp_url")
    @classmethod
    def _reject_unsafe_cdp_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            assert_public_url(
                value,
                allowed_schemes=("http", "https", "ws", "wss"),
                resolve_dns=False,
            )
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
        max_length=MAX_SELECTORS_PER_TARGET,
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
            assert_public_url(value, resolve_dns=False)
        except UnsafeURLError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("item_selector")
    @classmethod
    def _validate_item_selector(cls, value: SelectorValue | None) -> SelectorValue | None:
        if isinstance(value, str):
            _validate_short_selector_query(value)
        return value

    @field_validator("selectors")
    @classmethod
    def _validate_selectors(
        cls,
        value: dict[str, SelectorValue],
    ) -> dict[str, SelectorValue]:
        for name, selector in value.items():
            if not name or len(name) > MAX_SELECTOR_FIELD_NAME_CHARS:
                raise ValueError(
                    "Selector field names must contain between 1 and "
                    f"{MAX_SELECTOR_FIELD_NAME_CHARS} characters"
                )
            if isinstance(selector, str):
                _validate_short_selector_query(selector)
        return value

    @model_validator(mode="after")
    def _reject_browser_config_for_basic_fetcher(self) -> TargetConfig:
        if self.fetcher is FetcherType.basic and self.browser is not None:
            raise ValueError("browser configuration requires a dynamic or stealthy fetcher")
        return self


class RetryConfig(StrictConfigModel):
    """Retry policy configuration."""

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=MAX_RETRY_ATTEMPTS,
        description="Total request attempts, including the initial attempt",
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
        default_factory=list,
        max_length=MAX_REQUIRED_FIELDS,
        description="Fields that must be non-empty",
    )
    min_results: int = Field(default=0, ge=0, description="Minimum number of results expected")
    on_empty: OnEmptyAction = Field(
        default=OnEmptyAction.warn, description="Action when selectors return empty"
    )

    @field_validator("required_fields")
    @classmethod
    def _validate_required_fields(cls, value: list[str]) -> list[str]:
        return _validate_pattern_list(
            value,
            label="required_fields",
            max_item_chars=MAX_SELECTOR_FIELD_NAME_CHARS,
            case_insensitive=False,
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
        description=(
            "Minimum seconds between top-level fetch/navigation attempts to the same host; "
            "browser subresources are outside this limit"
        ),
    )
    mode: ExecutionMode = Field(default=ExecutionMode.auto, description="Response mode")
    priority: Priority = Field(default=Priority.normal, description="Queue priority")
    fail_strategy: FailStrategy = Field(
        default=FailStrategy.partial, description="How to handle target failures"
    )


class ScheduleConfig(StrictConfigModel):
    """Cron-style scheduling configuration."""

    cron: str = Field(..., description="Cron expression")
    timezone: str = Field(
        default="UTC",
        description="IANA timezone used to interpret the cron expression",
    )
    enabled: bool = Field(default=True, description="Whether the schedule is active")

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid IANA timezone: {value!r}") from exc
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
    headers: dict[str, str] = Field(default_factory=dict, description="Custom HTTP headers")
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
            assert_public_url(str(value), resolve_dns=False)
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
    _resolved_secret_values: tuple[str, ...] = PrivateAttr(default=())

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
        if (
            self.output.group_by == GroupBy.merge
            and self.targets is not None
            and any("_source" in target.selectors for target in self.targets)
        ):
            raise ValueError(
                "Selector field name '_source' is reserved when "
                "output.group_by is 'merge'"
            )
        return self

    def resolved_targets(self) -> list[TargetConfig]:
        """Return the list of targets regardless of Tier 1 or Tier 2 config."""
        if self.targets is None:
            raise RuntimeError("ScrapeConfig targets were not normalized")
        return self.targets

    @property
    def resolved_secret_values(self) -> tuple[str, ...]:
        """Deployment-secret values used only by run-scoped redaction."""

        return self._resolved_secret_values
