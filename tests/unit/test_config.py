"""Unit tests for config schema validators, transforms, and loader."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from scrapeyard.common.settings import get_settings
from scrapeyard.config import (
    BrowserActionType,
    MapDetectionConfig,  # noqa: F401
    ScrapeConfig,
    StockDetectionConfig,  # noqa: F401
    TargetConfig,
    WebhookConfig,
    WebhookStatus,
    apply_transforms,
    load_config,
    parse_transform,
    parse_transform_pipeline,
)
from scrapeyard.config.schema import (
    MAX_BROWSER_ACTIONS,
    MAX_BROWSER_ACTION_REPEAT,
    MAX_BROWSER_TIMEOUT_MS,
    MAX_BROWSER_WAIT_MS,
    MAX_DETECTION_CSS_SELECTORS,
    MAX_DETECTION_PATTERNS,
    MAX_DETECTION_TEXT_PATTERN_CHARS,
    MAX_DOMAIN_RATE_LIMIT_SECONDS,
    MAX_EXECUTION_CONCURRENCY,
    MAX_EXECUTION_DELAY_SECONDS,
    MAX_PAGINATION_PAGES,
    MAX_RETRY_ATTEMPTS,
    MAX_RETRY_BACKOFF_SECONDS,
    MAX_RETRYABLE_STATUSES,
    MAX_REQUIRED_FIELDS,
    MAX_TARGETS_PER_JOB,
    MAX_WEBHOOK_TIMEOUT_SECONDS,
    BrowserConfig,
    ExecutionConfig,
    PaginationConfig,
    RetryConfig,
    StockPatternConfig,
    ValidationConfig,
)


# --- Helpers ---


def _target_dict(**overrides) -> dict:
    base = {"url": "https://example.com", "selectors": {"title": "h1"}}
    base.update(overrides)
    return base


def _tier1_config(**overrides) -> dict:
    base = {"project": "test", "name": "job1", "target": _target_dict()}
    base.update(overrides)
    return base


def _tier2_config(**overrides) -> dict:
    base = {
        "project": "test",
        "name": "job2",
        "targets": [_target_dict(), _target_dict(url="https://example.org")],
    }
    base.update(overrides)
    return base


# --- Schema Validation ---


class TestScrapeConfigValidation:
    """Mutual-exclusivity validation for target/targets."""

    def test_valid_tier1_single_target(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target is not None
        assert config.targets == [config.target]

    def test_valid_tier2_multiple_targets(self):
        config = ScrapeConfig(**_tier2_config())
        assert config.targets is not None
        assert len(config.targets) == 2

    def test_both_target_and_targets_raises(self):
        data = _tier1_config(targets=[_target_dict()])
        with pytest.raises(ValidationError, match="not both"):
            ScrapeConfig(**data)

    def test_neither_target_nor_targets_raises(self):
        data = {"project": "test", "name": "job1"}
        with pytest.raises(ValidationError, match="must be provided"):
            ScrapeConfig(**data)

    def test_empty_targets_raises(self):
        data = {"project": "test", "name": "job1", "targets": []}
        with pytest.raises(ValidationError):
            ScrapeConfig(**data)

    def test_target_selectors_must_not_be_empty(self):
        data = _tier1_config(target=_target_dict(selectors={}))
        with pytest.raises(ValidationError):
            ScrapeConfig(**data)

    @pytest.mark.parametrize("field", ["project", "name"])
    def test_project_and_name_reject_path_components(self, field):
        data = _tier1_config(**{field: "../outside"})
        with pytest.raises(ValidationError, match="Unsafe project/name"):
            ScrapeConfig(**data)

    def test_schedule_rejects_invalid_cron_expression(self):
        data = _tier1_config(schedule={"cron": "not a cron", "enabled": True})
        with pytest.raises(ValidationError, match="Invalid cron expression"):
            ScrapeConfig(**data)

    def test_schedule_defaults_to_utc_and_accepts_iana_timezone(self):
        defaulted = ScrapeConfig(**_tier1_config(schedule={"cron": "0 9 * * *", "enabled": True}))
        explicit = ScrapeConfig(
            **_tier1_config(
                schedule={
                    "cron": "0 9 * * *",
                    "timezone": "America/New_York",
                    "enabled": True,
                }
            )
        )
        assert defaulted.schedule is not None
        assert defaulted.schedule.timezone == "UTC"
        assert explicit.schedule is not None
        assert explicit.schedule.timezone == "America/New_York"

    def test_schedule_rejects_unknown_timezone(self):
        data = _tier1_config(
            schedule={
                "cron": "0 9 * * *",
                "timezone": "Mars/Olympus_Mons",
            }
        )
        with pytest.raises(ValidationError, match="Invalid IANA timezone"):
            ScrapeConfig(**data)

    def test_unknown_top_level_config_key_raises(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScrapeConfig(**_tier1_config(typo_mode="sync"))

    def test_deployment_secret_references_resolve_at_load_time(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_PROXY_URL",
            "https://user:proxy-secret@proxy.example.com:8443",
        )
        monkeypatch.setenv("SCRAPEYARD_SECRET_WEBHOOK_AUTH", "Bearer hook-secret")
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
            '{"secret-ref":["SCRAPEYARD_SECRET_PROXY_URL",'
            '"SCRAPEYARD_SECRET_WEBHOOK_AUTH"]}',
        )
        get_settings.cache_clear()
        config = load_config(
            """
project: secret-ref
name: resolved
proxy:
  url: ${SCRAPEYARD_SECRET_PROXY_URL}
webhook:
  url: https://hooks.example.com/callback
  headers:
    Authorization: ${SCRAPEYARD_SECRET_WEBHOOK_AUTH}
target:
  url: https://example.com
  selectors:
    title: h1
"""
        )
        assert config.proxy is not None
        assert "proxy-secret" in config.proxy.url
        assert config.webhook is not None
        assert config.webhook.headers["Authorization"] == "Bearer hook-secret"

    def test_missing_deployment_secret_reference_fails_closed(self, monkeypatch):
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
            '{"secret-ref":["SCRAPEYARD_SECRET_MISSING"]}'
        )
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="SCRAPEYARD_SECRET_MISSING"):
            load_config(
                """
project: secret-ref
name: missing
target:
  url: https://example.com
  browser:
    extra_headers:
      Authorization: ${SCRAPEYARD_SECRET_MISSING}
  selectors:
    title: h1
"""
            )

    def test_deployment_secret_references_are_denied_without_project_allowlist(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("SCRAPEYARD_SECRET_CROSS_PROJECT", "Bearer exposed")
        with pytest.raises(ValueError, match="not allowed for project 'alpha'"):
            load_config(
                """
project: alpha
name: denied
webhook:
  url: https://attacker.example/callback
  headers:
    Authorization: ${SCRAPEYARD_SECRET_CROSS_PROJECT}
target:
  url: https://example.com
  selectors:
    title: h1
"""
            )

    def test_global_deployment_secret_allowlist_applies_to_every_project(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("SCRAPEYARD_SECRET_SHARED", "shared-value")
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
            '{"*":["SCRAPEYARD_SECRET_SHARED"]}',
        )
        get_settings.cache_clear()
        config = load_config(
            """
project: alpha
name: shared
target:
  url: https://example.com
  browser:
    extra_headers:
      X-Shared: ${SCRAPEYARD_SECRET_SHARED}
  selectors:
    title: h1
"""
        )
        assert config.target is not None
        assert config.target.browser.extra_headers["X-Shared"] == "shared-value"

    def test_project_cannot_be_derived_from_a_secret_reference(self, monkeypatch):
        monkeypatch.setenv("SCRAPEYARD_SECRET_PROJECT", "alpha")
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
            '{"alpha":["SCRAPEYARD_SECRET_PROJECT"]}',
        )
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="project must be a literal string"):
            load_config(
                """
project: ${SCRAPEYARD_SECRET_PROJECT}
name: hidden-project
target:
  url: https://example.com
  selectors:
    title: h1
"""
            )

    def test_name_cannot_be_derived_from_a_secret_reference(self, monkeypatch):
        monkeypatch.setenv("SCRAPEYARD_SECRET_NAME", "hidden-name")
        monkeypatch.setenv(
            "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
            '{"alpha":["SCRAPEYARD_SECRET_NAME"]}',
        )
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="name must be a literal string"):
            load_config(
                """
project: alpha
name: ${SCRAPEYARD_SECRET_NAME}
target:
  url: https://example.com
  selectors:
    title: h1
"""
            )

    def test_unknown_nested_config_key_raises(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScrapeConfig(**_tier1_config(target=_target_dict(fetchre="dynamic")))

    def test_unknown_selector_long_form_key_raises(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScrapeConfig(
                **_tier1_config(
                    target=_target_dict(selectors={"title": {"query": "h1", "unknown": True}})
                )
            )

    def test_long_form_selector_rejects_whitespace_only_query(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            ScrapeConfig(
                **_tier1_config(
                    target=_target_dict(
                        selectors={"title": {"query": "   ", "type": "css"}}
                    )
                )
            )


class TestResolvedTargets:
    """resolved_targets() convenience method."""

    def test_tier1_returns_single_element_list(self):
        config = ScrapeConfig(**_tier1_config())
        result = config.resolved_targets()
        assert len(result) == 1
        assert isinstance(result[0], TargetConfig)
        assert result is config.targets

    def test_tier2_returns_targets_list(self):
        config = ScrapeConfig(**_tier2_config())
        result = config.resolved_targets()
        assert len(result) == 2
        assert result is config.targets

    def test_target_with_item_selector_parses(self):
        config = ScrapeConfig(**_tier1_config(target=_target_dict(item_selector=".product-card")))
        assert config.target is not None
        assert config.target.item_selector == ".product-card"

    def test_target_with_browser_and_adaptive_domain_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    adaptive_domain="example.com",
                    browser={
                        "timeout_ms": 90000,
                        "disable_resources": False,
                        "network_idle": True,
                        "click_selector": "button.accept-age-gate",
                        "click_timeout_ms": 3000,
                        "click_wait_ms": 750,
                        "wait_for_selector": ".product-card a",
                        "wait_ms": 1500,
                    },
                )
            )
        )
        assert config.target is not None
        assert config.target.adaptive_domain == "example.com"
        assert config.target.browser is not None
        assert config.target.browser.timeout_ms == 90000
        assert config.target.browser.disable_resources is False
        assert config.target.browser.network_idle is True
        assert config.target.browser.click_selector == "button.accept-age-gate"
        assert config.target.browser.click_timeout_ms == 3000
        assert config.target.browser.click_wait_ms == 750
        assert config.target.browser.wait_for_selector == ".product-card a"
        assert config.target.browser.wait_ms == 1500

    def test_target_with_browser_actions_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    browser={
                        "actions": [
                            {"type": "click", "selector": "#accept", "optional": True},
                            {
                                "type": "scroll",
                                "times": 2,
                                "pixels": 900,
                                "wait_ms": 250,
                            },
                            {
                                "type": "repeat_click",
                                "selector": "button.load-more",
                                "max_times": 3,
                                "wait_for_selector": ".product-card",
                                "optional": True,
                            },
                        ]
                    },
                )
            )
        )

        assert config.target is not None
        assert config.target.browser is not None
        assert len(config.target.browser.actions) == 3
        assert config.target.browser.actions[0].type == BrowserActionType.click
        assert config.target.browser.actions[1].type == BrowserActionType.scroll
        assert config.target.browser.actions[1].times == 2
        assert config.target.browser.actions[2].max_times == 3

    def test_browser_action_missing_required_selector_raises(self):
        with pytest.raises(ValidationError, match="requires 'selector'"):
            ScrapeConfig(
                **_tier1_config(target=_target_dict(browser={"actions": [{"type": "click"}]}))
            )

    def test_browser_cdp_url_rejects_local_destinations(self):
        with pytest.raises(ValidationError, match="non-public"):
            BrowserConfig(cdp_url="ws://127.0.0.1:9222/devtools/browser/abc")

    def test_browser_extra_headers_reject_invalid_name(self):
        with pytest.raises(ValidationError, match="Invalid HTTP header name"):
            BrowserConfig(extra_headers={"Bad Header": "value"})

    def test_browser_extra_headers_reject_crlf_value(self):
        with pytest.raises(ValidationError, match="CR, LF, or NUL"):
            BrowserConfig(extra_headers={"X-Test": "ok\r\nX-Evil: 1"})

    def test_browser_extra_headers_reject_client_managed_names(self):
        with pytest.raises(ValidationError, match="managed by the HTTP client"):
            BrowserConfig(extra_headers={"Host": "internal.example"})

    def test_browser_extra_headers_reject_case_insensitive_duplicates(self):
        with pytest.raises(ValidationError, match="Duplicate HTTP header name"):
            BrowserConfig(extra_headers={"X-Test": "one", "x-test": "two"})

    def test_browser_useragent_rejects_crlf_value(self):
        with pytest.raises(ValidationError, match="CR, LF, or NUL"):
            BrowserConfig(useragent="Mozilla\r\nX-Evil: 1")

    @pytest.mark.parametrize(
        "option",
        [
            "addons",
            "args",
            "env",
            "executable_path",
            "ignore_default_args",
            "persistent_context",
            "proxy",
        ],
    )
    def test_browser_additional_arguments_rejects_process_controls(self, option):
        with pytest.raises(ValidationError, match="unsupported or unsafe"):
            BrowserConfig(additional_arguments={option: "attacker-controlled"})

    def test_browser_additional_arguments_accepts_yaml_safe_controls(self):
        config = BrowserConfig(
            additional_arguments={
                "locale": ["en-US", "en"],
                "fonts": ["Arial", "Noto Sans"],
                "custom_fonts_only": True,
                "window": [1280, 720],
            }
        )

        assert config.additional_arguments["window"] == [1280, 720]

    @pytest.mark.parametrize(
        "option",
        ["fingerprint", "screen", "webgl_config", "Locale"],
    )
    def test_browser_additional_arguments_rejects_non_yaml_or_mixed_case_options(
        self,
        option,
    ):
        with pytest.raises(ValidationError, match="unsupported or unsafe"):
            BrowserConfig(additional_arguments={option: {}})

    @pytest.mark.parametrize(
        "arguments, message",
        [
            ({"locale": 42}, "locale tag"),
            ({"locale": []}, "between 1 and"),
            ({"locale": "en-" + ("a" * 65)}, "locale tag"),
            ({"fonts": "Arial"}, "must be a list"),
            ({"fonts": [""]}, "invalid font name"),
            ({"custom_fonts_only": "yes"}, "must be a boolean"),
            ({"custom_fonts_only": True}, "requires at least one font"),
            ({"window": [1280]}, "two-item list"),
            ({"window": [True, 720]}, "positive integer dimensions"),
            ({"window": [1280, 20_000]}, "positive integer dimensions"),
        ],
    )
    def test_browser_additional_arguments_validates_values(
        self,
        arguments,
        message,
    ):
        with pytest.raises(ValidationError, match=message):
            BrowserConfig(additional_arguments=arguments)

    def test_long_form_selector_transform_is_validated_at_config_load(self):
        with pytest.raises(ValidationError, match="Invalid selector transform"):
            ScrapeConfig(
                **_tier1_config(
                    target=_target_dict(
                        selectors={
                            "price": {
                                "query": ".price",
                                "transform": "regex:[0-9+",
                            }
                        }
                    )
                )
            )

    def test_long_form_selector_transform_preserves_pipes_in_arguments(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    selectors={
                        "title": {
                            "query": "h1",
                            "transform": 'regex("a|b", "X")|append("|done")',
                        }
                    }
                )
            )
        )

        assert config.target is not None
        selector = config.target.selectors["title"]
        assert not isinstance(selector, str)
        assert selector.transform == 'regex("a|b", "X")|append("|done")'

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            (RetryConfig, {"max_attempts": 0}),
            (ValidationConfig, {"min_results": -1}),
            (ExecutionConfig, {"concurrency": 0}),
            (ExecutionConfig, {"delay_between": -1}),
            (ExecutionConfig, {"domain_rate_limit": -1}),
            (WebhookConfig, {"url": "https://example.com/hook", "timeout": 0}),
            (BrowserConfig, {"click_timeout_ms": 0}),
            (
                BrowserConfig,
                {"actions": [{"type": "click", "selector": "button", "timeout_ms": 0}]},
            ),
        ],
    )
    def test_numeric_runtime_config_rejects_values_that_can_hang_or_spin(self, model, kwargs):
        with pytest.raises(ValidationError):
            model(**kwargs)

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            (BrowserConfig, {"timeout_ms": MAX_BROWSER_TIMEOUT_MS + 1}),
            (BrowserConfig, {"click_timeout_ms": MAX_BROWSER_TIMEOUT_MS + 1}),
            (BrowserConfig, {"click_wait_ms": MAX_BROWSER_WAIT_MS + 1}),
            (BrowserConfig, {"wait_ms": MAX_BROWSER_WAIT_MS + 1}),
            (
                BrowserConfig,
                {"actions": [{"type": "scroll"} for _ in range(MAX_BROWSER_ACTIONS + 1)]},
            ),
            (
                BrowserConfig,
                {"actions": [{"type": "scroll", "times": MAX_BROWSER_ACTION_REPEAT + 1}]},
            ),
            (
                BrowserConfig,
                {
                    "actions": [
                        {
                            "type": "repeat_click",
                            "selector": "button",
                            "max_times": MAX_BROWSER_ACTION_REPEAT + 1,
                        }
                    ]
                },
            ),
            (ExecutionConfig, {"concurrency": MAX_EXECUTION_CONCURRENCY + 1}),
            (ExecutionConfig, {"delay_between": MAX_EXECUTION_DELAY_SECONDS + 1}),
            (ExecutionConfig, {"domain_rate_limit": MAX_DOMAIN_RATE_LIMIT_SECONDS + 1}),
            (PaginationConfig, {"next": "a.next", "max_pages": MAX_PAGINATION_PAGES + 1}),
            (RetryConfig, {"max_attempts": MAX_RETRY_ATTEMPTS + 1}),
            (RetryConfig, {"backoff_max": MAX_RETRY_BACKOFF_SECONDS + 1}),
            (RetryConfig, {"retryable_status": [500] * (MAX_RETRYABLE_STATUSES + 1)}),
            (
                WebhookConfig,
                {"url": "https://example.com/hook", "timeout": MAX_WEBHOOK_TIMEOUT_SECONDS + 1},
            ),
        ],
    )
    def test_numeric_runtime_config_rejects_unbounded_resource_values(self, model, kwargs):
        with pytest.raises(ValidationError):
            model(**kwargs)

    def test_targets_rejects_unbounded_target_lists(self):
        targets = [
            _target_dict(url=f"https://example.com/{idx}") for idx in range(MAX_TARGETS_PER_JOB + 1)
        ]

        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(target=None, targets=targets))

    def test_retryable_status_rejects_invalid_http_status_codes(self):
        with pytest.raises(ValidationError, match="valid HTTP status codes"):
            RetryConfig(retryable_status=[99, 600])


# --- Transform Parser ---


class TestParseTransform:
    """parse_transform() for all 8 transform types."""

    def test_trim(self):
        fn = parse_transform("trim")
        assert fn("  hello  ") == "hello"

    def test_lowercase(self):
        fn = parse_transform("lowercase")
        assert fn("HELLO") == "hello"

    def test_collapse_whitespace(self):
        fn = parse_transform("collapse_whitespace")
        assert fn("  hello \n\t world  ") == "hello world"

    def test_uppercase(self):
        fn = parse_transform("uppercase")
        assert fn("hello") == "HELLO"

    def test_no_arg_function_syntax(self):
        assert parse_transform("trim()")("  hello  ") == "hello"
        assert parse_transform("lowercase()")("HELLO") == "hello"

    def test_prepend(self):
        fn = parse_transform("prepend:prefix_")
        assert fn("value") == "prefix_value"

    def test_append(self):
        fn = parse_transform("append:_suffix")
        assert fn("value") == "value_suffix"

    def test_replace(self):
        fn = parse_transform("replace:old:new")
        assert fn("old value old") == "new value new"

    def test_replace_func_syntax_preserves_quoted_commas(self):
        fn = parse_transform('replace("old,value", "new")')
        assert fn("old,value here") == "new here"

    def test_malformed_function_arguments_raise(self):
        with pytest.raises(ValueError, match="Invalid transform arguments"):
            parse_transform('prepend("unterminated)')

    def test_regex(self):
        fn = parse_transform(r"regex:\d+:NUM")
        assert fn("item 42 and 7") == "item NUM and NUM"

    def test_pipeline_parser_preserves_regex_alternation_and_literal_pipe(self):
        transforms = parse_transform_pipeline(
            'regex("a|b", "X")|append("|done")'
        )

        assert apply_transforms("a b", transforms) == "X X|done"

    def test_pipeline_parser_handles_regex_character_class_pipe(self):
        transforms = parse_transform_pipeline("extract:[|]|append:(")

        assert apply_transforms("before|after", transforms) == "|("

    def test_pipeline_parser_does_not_treat_regex_replacement_as_pattern(self):
        transforms = parse_transform_pipeline("regex:a:[|trim")

        assert apply_transforms(" a ", transforms) == "["

    @pytest.mark.parametrize(
        "pipeline",
        ['append("unterminated)|trim', "trim|", "trim||lowercase"],
    )
    def test_pipeline_parser_rejects_malformed_or_blank_steps(self, pipeline):
        with pytest.raises(ValueError):
            parse_transform_pipeline(pipeline)

    def test_regex_rejects_oversized_patterns(self, monkeypatch):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(
                transform_regex_max_pattern_bytes=4,
                transform_regex_timeout_seconds=0.1,
            ),
        )

        with pytest.raises(ValueError, match="exceeds 4 UTF-8 bytes"):
            parse_transform("regex:12345:x")

    def test_regex_times_out_catastrophic_backtracking(self, monkeypatch):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(
                transform_regex_max_pattern_bytes=2048,
                transform_regex_timeout_seconds=0.001,
            ),
        )
        fn = parse_transform("regex:(a|aa)+$:x")

        with pytest.raises(ValueError, match="timed out"):
            fn("a" * 20_000 + "!")

    def test_regex_replacement_rejects_predicted_output_growth(self, monkeypatch):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(
                transform_regex_max_pattern_bytes=2048,
                transform_regex_timeout_seconds=0.1,
                transform_max_value_bytes=1024,
            ),
        )
        fn = parse_transform("regex:a:" + ("x" * 300))

        with pytest.raises(ValueError, match="exceeds 1024 UTF-8 bytes"):
            fn("aaaa")

    def test_remove(self):
        fn = parse_transform("remove:$")
        assert fn("$12.99") == "12.99"

    def test_strip_prefix(self):
        fn = parse_transform("strip_prefix:SKU-")
        assert fn("SKU-123") == "123"

    def test_strip_suffix(self):
        fn = parse_transform("strip_suffix: USD")
        assert fn("12.99 USD") == "12.99"

    def test_extract_returns_first_capture_group(self):
        fn = parse_transform(r"extract:(\d+\.\d+)")
        assert fn("price: 12.99 USD") == "12.99"

    def test_default_replaces_blank_values(self):
        fn = parse_transform("default:unknown")
        assert fn("   ") == "unknown"
        assert fn("present") == "present"

    def test_join_raises_not_supported(self):
        with pytest.raises(ValueError, match="list-level operation"):
            parse_transform("join:,")

    def test_unknown_transform_raises(self):
        with pytest.raises(ValueError, match="Unknown transform"):
            parse_transform("bogus")

    def test_prepend_missing_value_raises(self):
        with pytest.raises(ValueError, match="prepend requires"):
            parse_transform("prepend")


class TestApplyTransforms:
    """apply_transforms() chaining behavior."""

    def test_chains_multiple_transforms(self):
        transforms = [
            parse_transform("trim"),
            parse_transform("lowercase"),
            parse_transform("append:!"),
        ]
        assert apply_transforms("  HELLO  ", transforms) == "hello!"

    def test_empty_list_returns_unchanged(self):
        assert apply_transforms("value", []) == "value"

    def test_literal_replacement_stops_exponential_growth_before_allocation(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(transform_max_value_bytes=1024),
        )
        transforms = [parse_transform("replace:a:aa") for _ in range(11)]

        with pytest.raises(
            ValueError,
            match=r"exceeds 1024 UTF-8 bytes \(predicted 2048\)",
        ):
            apply_transforms("a", transforms)

    def test_initial_multibyte_value_is_bounded_by_utf8_bytes(self, monkeypatch):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(transform_max_value_bytes=1024),
        )

        with pytest.raises(ValueError, match="exceeds 1024 UTF-8 bytes"):
            apply_transforms("é" * 513, [])

    def test_config_rejects_excessive_transform_pipeline_steps(self, monkeypatch):
        monkeypatch.setattr(
            "scrapeyard.config.transforms.get_settings",
            lambda: SimpleNamespace(
                transform_max_pipeline_steps=2,
                transform_regex_max_pattern_bytes=2048,
                transform_regex_timeout_seconds=0.1,
            ),
        )

        with pytest.raises(ValidationError, match="pipeline exceeds 2 steps"):
            ScrapeConfig(
                **_tier1_config(
                    target=_target_dict(
                        selectors={
                            "title": {
                                "query": "h1",
                                "transform": "trim|lowercase|uppercase",
                            }
                        }
                    )
                )
            )


# --- YAML Loader ---


class TestLoadConfig:
    """load_config() YAML parsing integration."""

    def test_tier1_yaml(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
"""
        config = load_config(yaml_str)
        assert config.project == "demo"
        assert config.target is not None
        assert config.target.url == "https://example.com"

    def test_tier2_yaml(self):
        yaml_str = """
project: demo
name: job2
targets:
  - url: https://example.com
    selectors:
      title: h1
  - url: https://example.org
    selectors:
      heading: h2
"""
        config = load_config(yaml_str)
        assert config.targets is not None
        assert len(config.targets) == 2
        assert config.targets[1].url == "https://example.org"

    def test_invalid_both_target_and_targets_raises(self):
        yaml_str = """
project: demo
name: job3
target:
  url: https://example.com
  selectors:
    title: h1
targets:
  - url: https://example.org
    selectors:
      heading: h2
"""
        with pytest.raises(ValidationError, match="not both"):
            load_config(yaml_str)

    def test_rejects_yaml_aliases(self):
        yaml_str = """
project: demo
name: alias-job
selectors: &common_selectors
  title: h1
target:
  url: https://example.com
  selectors: *common_selectors
"""
        with pytest.raises(yaml.YAMLError, match="aliases"):
            load_config(yaml_str)

    def test_rejects_duplicate_yaml_keys(self):
        yaml_str = """
project: demo
project: other
name: duplicate-key-job
target:
  url: https://example.com
  selectors:
    title: h1
"""
        with pytest.raises(yaml.YAMLError, match="Duplicate YAML key"):
            load_config(yaml_str)

    def test_duplicate_yaml_key_error_does_not_echo_secret_key(self):
        yaml_str = """
secret-token: first
secret-token: second
"""
        with pytest.raises(yaml.YAMLError) as exc_info:
            load_config(yaml_str)

        assert "secret-token" not in str(exc_info.value)

    def test_rejects_unhashable_yaml_keys(self):
        with pytest.raises(yaml.YAMLError, match="mapping keys must be hashable"):
            load_config("? [project]\n: demo\n")

    def test_rejects_non_mapping_yaml_root(self):
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_config("- just\n- a\n- list")

    def test_webhook_on_key_does_not_require_quotes(self):
        yaml_str = """
project: demo
name: webhook-on-key
webhook:
  url: https://example.com/hook
  on: [complete, failed]
target:
  url: https://example.com
  selectors:
    title: h1
"""
        config = load_config(yaml_str)

        assert config.webhook is not None
        assert [status.value for status in config.webhook.on] == ["complete", "failed"]

    def test_true_false_values_still_parse_as_booleans(self):
        yaml_str = """
project: demo
name: bool-values
adaptive: true
schedule:
  cron: "0 * * * *"
  enabled: false
target:
  url: https://example.com
  selectors:
    title: h1
"""
        config = load_config(yaml_str)

        assert config.adaptive is True
        assert config.schedule is not None
        assert config.schedule.enabled is False

    def test_root_template_yaml_loads(self):
        template_path = Path(__file__).resolve().parents[2] / "template.yaml"
        config = load_config(template_path.read_text())
        assert config.project == "example-project"
        assert config.target is not None
        assert config.target.browser is not None
        assert config.target.adaptive_domain is None

    def test_root_template_yaml_matches_current_output_shape(self):
        template_path = Path(__file__).resolve().parents[2] / "template.yaml"
        raw = yaml.safe_load(template_path.read_text())

        assert raw["output"] == {"group_by": "target"}

    def test_dynamic_product_grid_example_loads_and_matches_current_shape(self):
        config_path = Path(__file__).resolve().parents[2] / "examples/dynamic-product-grid.yaml"
        raw = yaml.safe_load(config_path.read_text())
        config = load_config(config_path.read_text())

        assert raw["project"] == "demo"
        assert raw["name"] == "dynamic-product-grid"
        assert raw["adaptive"] is False
        assert raw["webhook"]["on"] == ["complete", "partial"]
        assert raw["output"] == {"group_by": "merge"}
        assert config.webhook is not None
        assert [status.value for status in config.webhook.on] == ["complete", "partial"]
        assert config.execution.fail_strategy.value == "partial"
        assert config.execution.delay_between == 2
        assert config.execution.domain_rate_limit == 2
        assert config.target is not None
        assert config.target.fetcher.value == "dynamic"
        assert "stock_signal" in raw["target"]["selectors"]
        assert "stock_status" not in raw["target"]["selectors"]
        assert raw["target"]["map_detection"]["text_patterns"]
        assert raw["target"]["stock_detection"]["in_stock"]["text_patterns"]
        assert config.target.pagination is not None
        assert config.target.pagination.max_pages == 2

    def test_dynamic_consent_scroll_example_loads_browser_actions(self):
        config_path = Path(__file__).resolve().parents[2] / "examples/dynamic-consent-scroll.yaml"
        config = load_config(config_path.read_text())

        assert config.target is not None
        assert config.target.browser is not None
        assert [action.type.value for action in config.target.browser.actions] == [
            "click",
            "wait_for_selector",
            "scroll",
        ]

    def test_load_more_product_grid_example_loads_repeat_click_action(self):
        config_path = Path(__file__).resolve().parents[2] / "examples/load-more-product-grid.yaml"
        config = load_config(config_path.read_text())

        assert config.target is not None
        assert config.target.browser is not None
        assert config.target.browser.actions[1].type.value == "repeat_click"
        assert config.target.browser.actions[1].max_times == 8

    def test_scheduled_product_monitor_example_keeps_detection_contract_in_parity(self):
        config_path = (
            Path(__file__).resolve().parents[2] / "examples/scheduled-product-monitor.yaml"
        )
        raw = yaml.safe_load(config_path.read_text())
        config = load_config(config_path.read_text())

        assert raw["project"] == "demo"
        assert raw["name"] == "scheduled-product-monitor"
        assert raw["adaptive"] is True
        assert raw["schedule"] == {"cron": "0 * * * *", "enabled": True}
        assert "stock_signal" in raw["target"]["selectors"]
        assert "stock_status" not in raw["target"]["selectors"]
        assert raw["target"]["map_detection"]["text_patterns"]
        assert raw["target"]["stock_detection"]["in_stock"]["text_patterns"]
        assert config.target is not None
        assert config.target.map_detection is not None
        assert config.target.stock_detection is not None


# --- Webhook Config ---


class TestWebhookConfig:
    """WebhookConfig schema parsing and validation."""

    def test_config_without_webhook_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.webhook is None

    def test_valid_webhook_parses(self):
        webhook_data = {
            "url": "https://example.com/hook",
            "on": ["complete", "failed"],
            "headers": {"Authorization": "Bearer token"},
            "timeout": 5,
        }
        config = ScrapeConfig(**_tier1_config(webhook=webhook_data))
        assert config.webhook is not None
        assert str(config.webhook.url) == "https://example.com/hook"
        assert config.webhook.on == [WebhookStatus.complete, WebhookStatus.failed]
        assert config.webhook.headers == {"Authorization": "Bearer token"}
        assert config.webhook.timeout == 5

    def test_webhook_defaults(self):
        webhook_data = {"url": "https://example.com/hook"}
        config = ScrapeConfig(**_tier1_config(webhook=webhook_data))
        assert config.webhook is not None
        assert config.webhook.on == [WebhookStatus.complete, WebhookStatus.partial]
        assert config.webhook.headers == {}
        assert config.webhook.timeout == 10

    def test_invalid_webhook_on_value_raises(self):
        webhook_data = {"url": "https://example.com/hook", "on": ["complet"]}
        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(webhook=webhook_data))

    def test_webhook_headers_reject_crlf_value(self):
        webhook_data = {
            "url": "https://example.com/hook",
            "headers": {"X-Test": "ok\r\nX-Evil: 1"},
        }
        with pytest.raises(ValidationError, match="CR, LF, or NUL"):
            ScrapeConfig(**_tier1_config(webhook=webhook_data))

    def test_webhook_headers_reject_client_managed_names(self):
        webhook_data = {
            "url": "https://example.com/hook",
            "headers": {"Transfer-Encoding": "chunked"},
        }
        with pytest.raises(ValidationError, match="managed by the HTTP client"):
            ScrapeConfig(**_tier1_config(webhook=webhook_data))

    def test_webhook_headers_reject_case_insensitive_duplicates(self):
        webhook_data = {
            "url": "https://example.com/hook",
            "headers": {"X-Test": "one", "x-test": "two"},
        }
        with pytest.raises(ValidationError, match="Duplicate HTTP header name"):
            ScrapeConfig(**_tier1_config(webhook=webhook_data))


class TestProxyConfig:
    """ProxyConfig schema parsing and precedence in target/job configs."""

    def test_target_with_proxy_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(proxy={"url": "http://user:pass@gate.example.com:7777"})
            )
        )
        assert config.target is not None
        assert config.target.proxy is not None
        assert config.target.proxy.url == "http://user:pass@gate.example.com:7777"

    def test_target_with_direct_proxy_parses(self):
        config = ScrapeConfig(**_tier1_config(target=_target_dict(proxy={"url": "direct"})))
        assert config.target.proxy.url == "direct"

    def test_proxy_url_is_trimmed(self):
        config = ScrapeConfig(**_tier1_config(proxy={"url": " http://gate.example.com:7777 "}))
        assert config.proxy is not None
        assert config.proxy.url == "http://gate.example.com:7777"

    def test_target_without_proxy_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.proxy is None

    def test_job_level_proxy_parses(self):
        config = ScrapeConfig(
            **_tier1_config(proxy={"url": "http://user:pass@gate.example.com:7777"})
        )
        assert config.proxy is not None
        assert config.proxy.url == "http://user:pass@gate.example.com:7777"

    def test_job_without_proxy_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.proxy is None

    def test_tier2_targets_with_mixed_proxy(self):
        config = ScrapeConfig(
            **_tier2_config(
                proxy={"url": "http://job-proxy:8080"},
                targets=[
                    _target_dict(proxy={"url": "http://target-proxy:9090"}),
                    _target_dict(url="https://example.org"),
                ],
            )
        )
        assert config.proxy.url == "http://job-proxy:8080"
        assert config.targets[0].proxy.url == "http://target-proxy:9090"
        assert config.targets[1].proxy is None

    def test_proxy_config_requires_url(self):
        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(proxy={}))

    @pytest.mark.parametrize(
        "proxy_url",
        [
            "proxy.example.com:8080",
            "file:///tmp/proxy.sock",
            "http:///missing-host",
            "http://gate.example.com:bad",
        ],
    )
    def test_invalid_proxy_url_raises(self, proxy_url):
        with pytest.raises(ValidationError):
            ScrapeConfig(**_tier1_config(proxy={"url": proxy_url}))

    def test_proxy_url_rejects_non_public_destination(self):
        with pytest.raises(ValidationError, match="non-public"):
            ScrapeConfig(**_tier1_config(proxy={"url": "http://127.0.0.1:8080"}))


class TestMapDetectionConfig:
    """MapDetectionConfig schema parsing and validation."""

    def test_target_with_map_detection_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    map_detection={
                        "text_patterns": ["add to cart to see price", "call for price"],
                        "css_selectors": [".map-price-message"],
                        "price_value_patterns": ["<hidden-price>"],
                    }
                )
            )
        )
        assert config.target.map_detection is not None
        assert len(config.target.map_detection.text_patterns) == 2
        assert len(config.target.map_detection.css_selectors) == 1
        assert len(config.target.map_detection.price_value_patterns) == 1

    def test_target_without_map_detection_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.map_detection is None

    def test_map_detection_empty_lists_default(self):
        config = ScrapeConfig(**_tier1_config(target=_target_dict(map_detection={})))
        assert config.target.map_detection is not None
        assert config.target.map_detection.text_patterns == []
        assert config.target.map_detection.css_selectors == []
        assert config.target.map_detection.price_value_patterns == []

    def test_map_detection_yaml_round_trip(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
  map_detection:
    text_patterns:
      - "add to cart to see price"
      - "call for price"
    css_selectors:
      - ".map-price-message"
    price_value_patterns:
      - "<hidden-price>"
"""
        config = load_config(yaml_str)
        assert config.target.map_detection is not None
        assert "add to cart to see price" in config.target.map_detection.text_patterns

    @pytest.mark.parametrize("field", ["text_patterns", "css_selectors"])
    @pytest.mark.parametrize("value", ["", "   "])
    def test_rejects_blank_match_patterns(self, field, value):
        with pytest.raises(ValidationError, match="must not be blank"):
            MapDetectionConfig(**{field: [value]})

    def test_preserves_explicit_empty_price_value_pattern(self):
        config = MapDetectionConfig(price_value_patterns=[""])
        assert config.price_value_patterns == [""]

    @pytest.mark.parametrize(
        ("field", "values"),
        [
            ("text_patterns", ["Call For Price", "call for price"]),
            ("css_selectors", [".price", ".price"]),
            ("price_value_patterns", ["hidden", "hidden"]),
        ],
    )
    def test_rejects_semantic_duplicates(self, field, values):
        with pytest.raises(ValidationError, match="must be unique"):
            MapDetectionConfig(**{field: values})

    def test_normalizes_case_insensitive_text_patterns_once(self):
        config = MapDetectionConfig(text_patterns=["  CALL FOR PRICE  "])
        assert config.text_patterns == ["call for price"]

    @pytest.mark.parametrize(
        ("field", "limit"),
        [
            ("text_patterns", MAX_DETECTION_PATTERNS),
            ("css_selectors", MAX_DETECTION_CSS_SELECTORS),
            ("price_value_patterns", MAX_DETECTION_PATTERNS),
        ],
    )
    def test_rejects_detection_collections_over_limit(self, field, limit):
        values = [f"pattern-{index}" for index in range(limit + 1)]
        with pytest.raises(ValidationError, match="too_long|at most"):
            MapDetectionConfig(**{field: values})

    def test_rejects_detection_pattern_over_item_limit(self):
        with pytest.raises(ValidationError, match="must not exceed"):
            MapDetectionConfig(
                text_patterns=["x" * (MAX_DETECTION_TEXT_PATTERN_CHARS + 1)]
            )


class TestStockDetectionConfig:
    """StockDetectionConfig schema parsing and validation."""

    def test_target_with_stock_detection_parses(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    stock_detection={
                        "in_stock": {"text_patterns": ["in stock", "available"]},
                        "out_of_stock": {"text_patterns": ["out of stock", "sold out"]},
                    }
                )
            )
        )
        assert config.target.stock_detection is not None
        assert config.target.stock_detection.in_stock is not None
        assert len(config.target.stock_detection.in_stock.text_patterns) == 2
        assert config.target.stock_detection.out_of_stock is not None
        assert config.target.stock_detection.limited_stock is None

    def test_target_without_stock_detection_defaults_to_none(self):
        config = ScrapeConfig(**_tier1_config())
        assert config.target.stock_detection is None

    def test_stock_detection_with_css_selectors(self):
        config = ScrapeConfig(
            **_tier1_config(
                target=_target_dict(
                    stock_detection={
                        "in_stock": {
                            "text_patterns": ["in stock"],
                            "css_selectors": [".in-stock-badge"],
                        },
                    }
                )
            )
        )
        sd = config.target.stock_detection
        assert sd.in_stock.css_selectors == [".in-stock-badge"]

    def test_stock_detection_yaml_round_trip(self):
        yaml_str = """
project: demo
name: job1
target:
  url: https://example.com
  selectors:
    title: h1
  stock_detection:
    in_stock:
      text_patterns: ["in stock"]
    out_of_stock:
      text_patterns: ["out of stock"]
"""
        config = load_config(yaml_str)
        assert config.target.stock_detection is not None
        assert config.target.stock_detection.in_stock is not None

    @pytest.mark.parametrize("field", ["text_patterns", "css_selectors"])
    def test_stock_patterns_reject_blank_values(self, field):
        with pytest.raises(ValidationError, match="must not be blank"):
            StockPatternConfig(**{field: ["  "]})

    def test_stock_text_patterns_reject_case_insensitive_duplicates(self):
        with pytest.raises(ValidationError, match="must be unique"):
            StockPatternConfig(text_patterns=["In Stock", "in stock"])


class TestValidationConfigBounds:
    def test_required_fields_reject_blank_and_duplicate_values(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            ValidationConfig(required_fields=[" "])
        with pytest.raises(ValidationError, match="must be unique"):
            ValidationConfig(required_fields=["price", "price"])

    def test_required_fields_rejects_collection_over_limit(self):
        with pytest.raises(ValidationError, match="too_long|at most"):
            ValidationConfig(
                required_fields=[f"field-{index}" for index in range(MAX_REQUIRED_FIELDS + 1)]
            )
