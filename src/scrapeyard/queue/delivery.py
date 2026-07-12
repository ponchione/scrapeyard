"""Shared construction of queue metadata from validated scrape config."""

from __future__ import annotations

from dataclasses import dataclass

from scrapeyard.config.schema import FetcherType, ScrapeConfig


@dataclass(frozen=True, slots=True)
class QueueDeliveryMetadata:
    """Queue fields that must match normal and recovered submissions."""

    priority: str
    needs_browser: bool


def queue_delivery_metadata(config: ScrapeConfig) -> QueueDeliveryMetadata:
    """Derive arq enqueue metadata from one validated stored config."""
    return QueueDeliveryMetadata(
        priority=config.execution.priority.value,
        needs_browser=any(
            target.fetcher != FetcherType.basic
            for target in config.resolved_targets()
        ),
    )
