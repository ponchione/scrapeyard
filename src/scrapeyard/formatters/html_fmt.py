"""HTML result formatter — returns raw HTML content."""

from __future__ import annotations

from typing import Any

from scrapeyard.config.schema import GroupBy


def format_html(
    job_meta: dict[str, Any],
    results: list[dict[str, Any]],
    group_by: GroupBy,
) -> str:
    """Return raw HTML content from scraped results.

    For HTML format, each target's data is expected to be raw HTML.
    Results are concatenated with target URL comments as separators.
    """
    parts: list[str] = []
    for entry in results:
        url = entry["url"]
        data = entry["data"]
        parts.append(f"<!-- source: {url} -->")
        parts.append(str(data))
    return "\n".join(parts)
