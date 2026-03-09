"""JSON result formatter (spec section 8.2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from scrapeyard.config.schema import GroupBy


def format_json(
    job_meta: dict[str, Any],
    results: list[dict[str, Any]],
    group_by: GroupBy,
) -> dict[str, Any]:
    """Format scraped data as a JSON structure.

    Parameters
    ----------
    job_meta:
        Job metadata (project, name, job_id, etc.).
    results:
        List of per-target result dicts, each containing ``"url"`` and ``"data"``.
    group_by:
        Grouping strategy — ``target`` groups by URL, ``merge`` flattens with ``_source``.
    """
    base: dict[str, Any] = {
        "job_id": job_meta.get("job_id"),
        "project": job_meta.get("project"),
        "status": job_meta.get("status", "complete"),
        "completed_at": job_meta.get("completed_at", datetime.now(timezone.utc).isoformat()),
        "errors": job_meta.get("errors", []),
    }

    if group_by == GroupBy.target:
        grouped: dict[str, Any] = {}
        for entry in results:
            url = entry["url"]
            key = urlparse(url).netloc or url
            data = entry["data"]
            count = len(data) if isinstance(data, list) else 1
            grouped[key] = {
                "status": entry.get("status", "success"),
                "count": count,
                "data": data,
            }
        return {**base, "results": grouped}

    # merge mode: flatten all records with _source field
    merged: list[dict[str, Any]] = []
    for entry in results:
        url = entry["url"]
        source = urlparse(url).netloc or url
        data = entry["data"]
        if isinstance(data, list):
            for record in data:
                merged.append({**record, "_source": source})
        else:
            merged.append({**data, "_source": source})
    return {**base, "results": merged}
