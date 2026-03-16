"""Webhook payload construction and firing logic."""

from __future__ import annotations

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.models.job import JobStatus


def should_fire(config: WebhookConfig, status: JobStatus) -> bool:
    """Return True if the webhook should fire for the given job status."""
    return status.value in {s.value for s in config.on}
