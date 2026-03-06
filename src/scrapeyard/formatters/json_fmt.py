"""JSON result formatter (spec section 8.2)."""

from __future__ import annotations

from typing import Any

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
    if group_by == GroupBy.target:
        grouped: dict[str, Any] = {}
        for entry in results:
            url = entry["url"]
            grouped[url] = entry["data"]
        return {**job_meta, "results": grouped}

    # merge mode: flatten all records with _source field
    merged: list[dict[str, Any]] = []
    for entry in results:
        url = entry["url"]
        data = entry["data"]
        if isinstance(data, list):
            for record in data:
                merged.append({**record, "_source": url})
        else:
            merged.append({**data, "_source": url})
    return {**job_meta, "results": merged}
