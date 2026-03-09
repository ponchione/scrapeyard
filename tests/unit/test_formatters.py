"""Tests for result formatters — JSON grouped, JSON merged, and Markdown."""

from __future__ import annotations

from scrapeyard.config.schema import GroupBy, OutputFormat
from scrapeyard.formatters.factory import get_formatter
from scrapeyard.formatters.html_fmt import format_html
from scrapeyard.formatters.json_fmt import format_json
from scrapeyard.formatters.markdown_fmt import format_markdown

_META = {"project": "acme", "name": "scrape-prices", "job_id": "j-1"}

_RESULTS = [
    {"url": "https://a.com", "data": [{"title": "A1", "price": 10}, {"title": "A2", "price": 20}]},
    {"url": "https://b.com", "data": [{"title": "B1", "price": 30}]},
]


class TestJsonGroupedByTarget:
    def test_groups_by_url(self):
        out = format_json(_META, _RESULTS, GroupBy.target)
        assert "a.com" in out["results"]
        assert "b.com" in out["results"]
        assert out["results"]["a.com"]["data"] == _RESULTS[0]["data"]
        assert out["results"]["a.com"]["status"] == "success"
        assert out["results"]["a.com"]["count"] == 2

    def test_includes_metadata(self):
        out = format_json(_META, _RESULTS, GroupBy.target)
        assert out["project"] == "acme"
        assert out["job_id"] == "j-1"


class TestJsonMerged:
    def test_flattens_with_source(self):
        out = format_json(_META, _RESULTS, GroupBy.merge)
        records = out["results"]
        assert len(records) == 3
        assert all("_source" in r for r in records)

    def test_source_field_values(self):
        out = format_json(_META, _RESULTS, GroupBy.merge)
        sources = [r["_source"] for r in out["results"]]
        assert sources == ["a.com", "a.com", "b.com"]

    def test_preserves_original_fields(self):
        out = format_json(_META, _RESULTS, GroupBy.merge)
        first = out["results"][0]
        assert first["title"] == "A1"
        assert first["price"] == 10

    def test_single_dict_data_gets_source(self):
        results = [{"url": "https://c.com", "data": {"title": "C1"}}]
        out = format_json(_META, results, GroupBy.merge)
        assert len(out["results"]) == 1
        assert out["results"][0]["_source"] == "c.com"


class TestMarkdown:
    def test_target_mode_has_headings(self):
        out = format_markdown(_META, _RESULTS, GroupBy.target)
        assert "## https://a.com" in out
        assert "## https://b.com" in out

    def test_target_mode_has_table(self):
        out = format_markdown(_META, _RESULTS, GroupBy.target)
        assert "| title | price |" in out
        assert "| A1 | 10 |" in out

    def test_merge_mode_has_source_column(self):
        out = format_markdown(_META, _RESULTS, GroupBy.merge)
        assert "| Source |" in out
        assert "| https://a.com |" in out

    def test_main_heading(self):
        out = format_markdown(_META, _RESULTS, GroupBy.target)
        assert out.startswith("# scrape-prices")


class TestHtml:
    def test_returns_raw_html(self):
        results = [{"url": "https://a.com", "data": "<html>hello</html>"}]
        out = format_html(_META, results, GroupBy.target)
        assert "<html>hello</html>" in out
        assert "<!-- source: https://a.com -->" in out


class TestFactory:
    def test_json_returns_json_formatter(self):
        f = get_formatter(OutputFormat.json, GroupBy.target)
        assert f is format_json

    def test_markdown_returns_markdown_formatter(self):
        f = get_formatter(OutputFormat.markdown, GroupBy.target)
        assert f is format_markdown

    def test_html_returns_html_formatter(self):
        f = get_formatter(OutputFormat.html, GroupBy.target)
        assert f is format_html

    def test_json_markdown_returns_json_formatter(self):
        f = get_formatter(OutputFormat.json_markdown, GroupBy.target)
        assert f is format_json
