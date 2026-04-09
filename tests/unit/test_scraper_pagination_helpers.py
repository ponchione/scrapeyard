from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.pagination import paginate_target
from scrapeyard.engine.scraper import FetchOutcome, TargetResult


class _Element:
    def __init__(self, href: str | None = None) -> None:
        self.attrib = {}
        if href is not None:
            self.attrib["href"] = href


class _Page:
    def __init__(self, next_links: list[_Element] | None = None) -> None:
        self._next_links = next_links or []

    def css(self, selector: str):
        assert selector == "a.next"
        return self._next_links


@pytest.mark.asyncio
async def test_paginate_target_fetches_follow_on_pages_and_updates_result():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 3},
        }
    )
    result = TargetResult(url=target.url, status="success", data=[{"title": "first"}], pages_scraped=1, debug={"final_url": target.url})
    page1 = _Page([_Element("/page-2")])
    page2 = _Page([])
    fetch_page = AsyncMock(return_value=FetchOutcome(page=page2, debug={"final_url": "https://example.com/page-2"}))
    extract_page_data = MagicMock(return_value=[{"title": "second"}])

    await paginate_target(
        page=page1,
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=extract_page_data,
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url="http://proxy:8080",
        artifacts_dir="/tmp/artifacts",
    )

    assert result.pages_scraped == 2
    assert result.data == [{"title": "first"}, {"title": "second"}]
    fetch_page.assert_awaited_once()
    assert fetch_page.await_args.args[2] == "https://example.com/page-2"


@pytest.mark.asyncio
async def test_paginate_target_noops_without_pagination_config():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
        }
    )
    result = TargetResult(url=target.url, status="success", data=[], pages_scraped=1, debug={})
    fetch_page = AsyncMock()

    await paginate_target(
        page=_Page([_Element("/page-2")]),
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=MagicMock(),
        retry_handler=MagicMock(),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status=set(),
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch_page.assert_not_called()
    assert result.pages_scraped == 1
