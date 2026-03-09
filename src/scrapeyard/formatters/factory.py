"""Formatter factory — returns the appropriate formatter for a given output config."""

from __future__ import annotations

from typing import Any, Callable

from scrapeyard.config.schema import GroupBy, OutputFormat
from scrapeyard.formatters.html_fmt import format_html
from scrapeyard.formatters.json_fmt import format_json
from scrapeyard.formatters.markdown_fmt import format_markdown

# Type alias for a formatter function.
Formatter = Callable[[dict[str, Any], list[dict[str, Any]], GroupBy], Any]


def get_formatter(format: OutputFormat, group_by: GroupBy) -> Formatter:
    """Return a formatter function for the given output format.

    Parameters
    ----------
    format:
        The desired output format.

    Returns
    -------
    Formatter
        A callable ``(job_meta, results, group_by) -> formatted_output``.
    """
    _ = group_by
    formatters: dict[OutputFormat, Formatter] = {
        OutputFormat.json: format_json,
        OutputFormat.markdown: format_markdown,
        OutputFormat.html: format_html,
        OutputFormat.json_markdown: format_json,  # primary is JSON; caller writes both
    }
    formatter = formatters.get(format)
    if formatter is None:
        raise ValueError(f"Unsupported output format: {format!r}")
    return formatter
