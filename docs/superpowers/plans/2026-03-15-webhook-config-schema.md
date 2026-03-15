# WebhookConfig Schema — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add WebhookStatus enum and WebhookConfig model to the YAML config schema, and promote httpx to production dependency.

**Architecture:** New enum and model in `schema.py` following existing Pydantic v2 patterns. Optional `webhook` field on `ScrapeConfig`. Exports added to `__init__.py`. httpx moved in `pyproject.toml`.

**Tech Stack:** Python, Pydantic v2, Poetry, pytest

**Spec:** `docs/superpowers/specs/2026-03-15-webhook-config-schema-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/scrapeyard/config/schema.py` | Modify | Add WebhookStatus enum, WebhookConfig model, webhook field on ScrapeConfig |
| `src/scrapeyard/config/__init__.py` | Modify | Export WebhookConfig and WebhookStatus |
| `pyproject.toml` | Modify | Move httpx to production dependencies |
| `tests/unit/test_config.py` | Modify | Add TestWebhookConfig test class |

---

## Chunk 1: Implementation

### Task 1: Write failing tests

**Files:**
- Modify: `tests/unit/test_config.py`

- [ ] **Step 1: Add imports and test class**

Add `WebhookConfig` and `WebhookStatus` to the import block in `test_config.py`, and add the `TestWebhookConfig` class at the end of the file:

```python
# Add to imports:
from scrapeyard.config import (
    ScrapeConfig,
    TargetConfig,
    WebhookConfig,
    WebhookStatus,
    apply_transforms,
    load_config,
    parse_transform,
)

# Add at end of file:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::TestWebhookConfig -v`

Expected: FAIL — `ImportError` because `WebhookConfig` and `WebhookStatus` don't exist yet.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/unit/test_config.py
git commit -m "test: add failing tests for WebhookConfig schema (WO-001)"
```

---

### Task 2: Implement schema changes

**Files:**
- Modify: `src/scrapeyard/config/schema.py`

- [ ] **Step 4: Add WebhookStatus enum**

Add after the `FailStrategy` enum (after line 83), before the `# --- Selector Models ---` comment:

```python
class WebhookStatus(str, Enum):
    """Job statuses that can trigger a webhook."""

    complete = "complete"
    partial = "partial"
    failed = "failed"
```

- [ ] **Step 5: Add WebhookConfig model**

Add after `OutputConfig` (after line 173), before the `# --- Top-Level Config ---` comment:

```python
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
```

- [ ] **Step 6: Add HttpUrl import**

Add `HttpUrl` to the pydantic import line:

```python
from pydantic import BaseModel, Field, HttpUrl, model_validator
```

- [ ] **Step 7: Add webhook field to ScrapeConfig**

Add between the `schedule` and `output` fields in `ScrapeConfig`:

```python
    webhook: Optional[WebhookConfig] = None
```

- [ ] **Step 8: Update __init__.py exports**

In `src/scrapeyard/config/__init__.py`, add to imports and `__all__`:

```python
from scrapeyard.config.schema import ScrapeConfig, TargetConfig, WebhookConfig, WebhookStatus

__all__ = [
    "ScrapeConfig",
    "TargetConfig",
    "WebhookConfig",
    "WebhookStatus",
    "apply_transforms",
    "load_config",
    "parse_transform",
]
```

- [ ] **Step 9: Run the new tests**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::TestWebhookConfig -v`

Expected: All 4 tests PASS.

- [ ] **Step 10: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`

Expected: All previously passing tests still pass (126/128, same 2 pre-existing failures).

- [ ] **Step 11: Commit schema changes**

```bash
git add src/scrapeyard/config/schema.py src/scrapeyard/config/__init__.py
git commit -m "feat: add WebhookStatus enum and WebhookConfig model to config schema (WO-001)"
```

---

### Task 3: Promote httpx to production dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 12: Move httpx in pyproject.toml**

Move `httpx = "^0.28"` from `[tool.poetry.group.dev.dependencies]` to `[tool.poetry.dependencies]`.

- [ ] **Step 13: Run poetry lock to update lockfile**

Run: `.venv/bin/python -m poetry lock --no-update` (or `poetry lock --no-update` if poetry is on PATH)

Expected: Lock file updated without changing dependency versions.

- [ ] **Step 14: Run tests to verify nothing broke**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`

Expected: All tests pass.

- [ ] **Step 15: Commit dependency change**

```bash
git add pyproject.toml poetry.lock
git commit -m "build: promote httpx to production dependency (WO-001)"
```

---

## Done

After all steps pass, WO-001 is complete. The next work order in sequence is WO-002 (SaveResultMeta dataclass).
