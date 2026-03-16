"""Tests for webhook payload construction and should_fire filtering."""

from __future__ import annotations

import pytest

from scrapeyard.config.schema import WebhookConfig, WebhookStatus
from scrapeyard.models.job import JobStatus
from scrapeyard.webhook.payload import should_fire


class TestShouldFire:
    def test_complete_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.complete) is True

    def test_partial_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.partial) is True

    def test_failed_not_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.failed) is False

    def test_custom_on_list(self):
        config = WebhookConfig(
            url="https://example.com/hook",
            on=[WebhookStatus.failed],
        )
        assert should_fire(config, JobStatus.failed) is True
        assert should_fire(config, JobStatus.complete) is False

    def test_non_terminal_status_never_fires(self):
        config = WebhookConfig(
            url="https://example.com/hook",
            on=[WebhookStatus.complete, WebhookStatus.partial, WebhookStatus.failed],
        )
        assert should_fire(config, JobStatus.queued) is False
        assert should_fire(config, JobStatus.running) is False
