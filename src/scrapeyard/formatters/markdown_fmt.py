"""Markdown table result formatter."""

from __future__ import annotations

from typing import Any

from scrapeyard.config.schema import GroupBy


def format_markdown(
    job_meta: dict[str, Any],
    results: list[dict[str, Any]],
    group_by: GroupBy,
) -> str:
    """Format scraped data as Markdown tables.

    Parameters
    ----------
    job_meta:
        Job metadata (project, name, job_id, etc.).
    results:
        List of per-target result dicts, each containing ``"url"`` and ``"data"``.
    group_by:
        Grouping strategy — ``target`` produces one table per URL,
        ``merge`` produces a single table with a Source column.
    """
    lines: list[str] = []
    lines.append(f"# {job_meta.get('name', 'Results')}")
    lines.append("")

    if group_by == GroupBy.target:
        for entry in results:
            url = entry["url"]
            data = entry["data"]
            lines.append(f"## {url}")
            lines.append("")
            lines.extend(_records_to_table(data))
            lines.append("")
    else:
        rows: list[dict[str, Any]] = []
        for entry in results:
            url = entry["url"]
            data = entry["data"]
            if isinstance(data, list):
                for record in data:
                    rows.append({**record, "Source": url})
            else:
                rows.append({**data, "Source": url})
        lines.extend(_records_to_table(rows))
        lines.append("")

    return "\n".join(lines)


def _records_to_table(data: Any) -> list[str]:
    """Convert a list of dicts (or a single dict) into Markdown table lines."""
    if not isinstance(data, list):
        data = [data]
    if not data:
        return ["*No data*"]

    headers = list(data[0].keys())
    lines: list[str] = []
    lines.append("| " + " | ".join(str(h) for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in data:
        cells = [str(row.get(h, "")) for h in headers]
        lines.append("| " + " | ".join(cells) + " |")
    return lines
