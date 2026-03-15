# Add WebhookConfig to YAML Config Schema

**Date:** 2026-03-15
**Work Order:** 001-webhook-config-schema

## Problem

Scrapeyard has no webhook support. The YAML config schema needs a `webhook` block so jobs can notify external services on completion. This is schema-only — no HTTP dispatch logic.

## Solution

Add `WebhookStatus` enum and `WebhookConfig` model to `schema.py`, add an optional `webhook` field to `ScrapeConfig`, and promote `httpx` to a production dependency.

## New Types

### WebhookStatus (str, Enum)

Values: `complete`, `partial`, `failed`

### WebhookConfig (BaseModel)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | `HttpUrl` | required | Pydantic's `HttpUrl` type provides URL validation |
| `on` | `list[WebhookStatus]` | `[complete, partial]` | Which job statuses trigger the webhook |
| `headers` | `dict[str, str]` | `{}` | Custom HTTP headers to include |
| `timeout` | `int` | `10` | Timeout in seconds for the webhook POST |

The `on` field validates that all values are valid `WebhookStatus` members; invalid values raise `ValidationError`.

## Changes

| File | Change |
|------|--------|
| `src/scrapeyard/config/schema.py` | Add `WebhookStatus` enum and `WebhookConfig` model. Add `webhook: Optional[WebhookConfig] = None` to `ScrapeConfig`. |
| `pyproject.toml` | Move `httpx = "^0.28"` from dev dependencies to production dependencies |
| `tests/unit/test_config.py` | Add `TestWebhookConfig` test class |

## Files NOT Changed

- `src/scrapeyard/config/loader.py` — Pydantic handles the new optional field automatically
- `src/scrapeyard/config/__init__.py` — no re-export needed for internal schema types
- Any existing models or enum values

## New Tests

`TestWebhookConfig` class in `test_config.py`:

1. `test_config_without_webhook_defaults_to_none` — parse a config with no webhook block, assert `webhook is None`
2. `test_valid_webhook_parses` — parse a config with a valid webhook block, assert `WebhookConfig` is populated with correct url/on/headers/timeout
3. `test_webhook_defaults` — parse a config with only `webhook.url`, assert defaults: `on=[complete, partial]`, `headers={}`, `timeout=10`
4. `test_invalid_webhook_on_value_raises` — parse a config with `on: [complet]`, assert `ValidationError`

## Design Decisions

- **Same file (`schema.py`):** Follows the existing convention that all config schema models and enums live in one file. At ~236 lines post-change, still well within reason.
- **`HttpUrl` for validation:** Pydantic v2 provides `HttpUrl` which validates URL format. Prevents misconfigured URLs from reaching the dispatcher.
- **`httpx` promoted now:** The webhook dispatcher (WO-003) will need it as a production dependency. Moving it now per WO-001 keeps the dependency change atomic and avoids WO-003 mixing schema and dependency concerns.

## Acceptance Criteria (from work order)

- [x] `WebhookStatus` enum with values: complete, partial, failed
- [x] `WebhookConfig` model with fields: url, on, headers, timeout (with correct types and defaults)
- [x] `on` field validates against `WebhookStatus`; invalid values raise `ValidationError`
- [x] `ScrapeConfig.webhook` is `Optional[WebhookConfig]` defaulting to `None`
- [x] YAML configs without webhook block parse with `webhook=None`
- [x] YAML configs with valid webhook block parse into populated `WebhookConfig`
- [x] YAML configs with invalid `webhook.on` values raise `ValidationError`
- [x] `httpx` listed under production dependencies in `pyproject.toml`
- [x] All existing unit tests pass
- [x] New unit tests cover: parsing, defaults, validation errors, omission
