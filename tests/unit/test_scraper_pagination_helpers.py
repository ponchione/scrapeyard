from __future__ import annotations

import asyncio
import socket
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.pagination import paginate_target
from scrapeyard.engine.scraper import FetchOutcome, TargetResult
from scrapeyard.engine.url_guard import URLResolutionError


class _Element:
    def __init__(self, href: str | None = None) -> None:
        self.attrib = {}
        if href is not None:
            self.attrib["href"] = href


class _Page:
    def __init__(
        self,
        next_links: list[_Element] | None = None,
        xpath_links: list[_Element] | None = None,
    ) -> None:
        self._next_links = next_links or []
        self._xpath_links = xpath_links or []

    def css(self, selector: str):
        assert selector == "a.next"
        return self._next_links

    def xpath(self, selector: str):
        assert selector == "//a[contains(., 'Next')]"
        return self._xpath_links


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
async def test_paginate_target_stops_when_next_url_is_current_page():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/products?page=1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 3},
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    fetch_page = AsyncMock(return_value=FetchOutcome(page=_Page([]), debug={"final_url": f"{target.url}#top"}))
    extract_page_data = MagicMock(return_value=[{"title": "duplicate"}])

    await paginate_target(
        page=_Page([_Element("#top")]),
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=extract_page_data,
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch_page.assert_not_awaited()
    extract_page_data.assert_not_called()
    assert result.pages_scraped == 1
    assert result.data == [{"title": "first"}]


@pytest.mark.asyncio
async def test_paginate_target_stops_when_next_url_is_unsafe():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/products?page=1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 3},
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    fetch_page = AsyncMock()

    await paginate_target(
        page=_Page([_Element("http://169.254.169.254/latest/meta-data")]),
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=MagicMock(return_value=[{"title": "metadata"}]),
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch_page.assert_not_awaited()
    assert result.pages_scraped == 1
    assert result.data == [{"title": "first"}]


@pytest.mark.asyncio
async def test_paginate_target_stops_when_next_url_was_seen_after_redirect():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 3},
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    fetch_page = AsyncMock(return_value=FetchOutcome(page=_Page([]), debug={"final_url": target.url}))
    extract_page_data = MagicMock(return_value=[{"title": "duplicate"}])

    await paginate_target(
        page=_Page([_Element("/page-2")]),
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=extract_page_data,
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch_page.assert_awaited_once()
    extract_page_data.assert_not_called()
    assert result.pages_scraped == 1
    assert result.data == [{"title": "first"}]


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


@pytest.mark.asyncio
async def test_paginate_target_supports_xpath_next_selector():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {
                "next": {"query": "//a[contains(., 'Next')]", "type": "xpath"},
                "max_pages": 2,
            },
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    fetch_page = AsyncMock(
        return_value=FetchOutcome(page=_Page(), debug={"final_url": "https://example.com/page-2"})
    )

    await paginate_target(
        page=_Page(xpath_links=[_Element("/page-2")]),
        target=target,
        result=result,
        fetch_target_page=fetch_page,
        extract_page_data=MagicMock(return_value=[{"title": "second"}]),
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch_page.assert_awaited_once()
    assert fetch_page.await_args.args[2] == "https://example.com/page-2"
    assert result.pages_scraped == 2


@pytest.mark.asyncio
async def test_paginate_target_enforces_aggregate_record_budget_across_pages():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}, {"title": "second"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    budget = RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=1000,
        max_extracted_records=3,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )
    await budget.consume_extracted_records(2)
    fetch_page = AsyncMock(
        return_value=FetchOutcome(
            page=_Page([]),
            debug={"final_url": "https://example.com/page-2"},
        )
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        await paginate_target(
            page=_Page([_Element("/page-2")]),
            target=target,
            result=result,
            fetch_target_page=fetch_page,
            extract_page_data=MagicMock(
                return_value=[{"title": "third"}, {"title": "fourth"}]
            ),
            retry_handler=MagicMock(spec=RetryConfig),
            fetcher_cls=object(),
            adaptive=False,
            retryable_status={500},
            adaptive_dir="/tmp/adaptive",
            proxy_url=None,
            artifacts_dir=None,
            budget=budget,
        )

    assert exc_info.value.limit_name is BudgetLimitName.extracted_records
    assert exc_info.value.observed_amount == 4
    assert result.data == [{"title": "first"}, {"title": "second"}]
    assert fetch_page.await_args.args[-1] is budget


@pytest.mark.asyncio
async def test_paginated_fetch_uses_one_overall_deadline():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "fetcher": FetcherType.basic,
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    result = TargetResult(
        url=target.url,
        status="success",
        data=[{"title": "first"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    budget = RunBudget(
        max_duration_seconds=0.01,
        max_fetched_bytes=1000,
        max_extracted_records=10,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )

    async def blocked_fetch(*args):
        passed_budget = args[-1]
        await passed_budget.wait_for(asyncio.Event().wait())

    with pytest.raises(BudgetExceeded) as exc_info:
        await paginate_target(
            page=_Page([_Element("/page-2")]),
            target=target,
            result=result,
            fetch_target_page=blocked_fetch,
            extract_page_data=MagicMock(),
            retry_handler=MagicMock(spec=RetryConfig),
            fetcher_cls=object(),
            adaptive=False,
            retryable_status=set(),
            adaptive_dir="/tmp/adaptive",
            proxy_url=None,
            artifacts_dir=None,
            budget=budget,
        )

    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds


@pytest.mark.asyncio
async def test_blocking_pagination_dns_does_not_block_event_loop(monkeypatch):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_resolver(*_args, **_kwargs):
        started.set()
        release.wait(timeout=1)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 0),
            )
        ]

    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        blocking_resolver,
    )
    fetch_page = AsyncMock(
        return_value=FetchOutcome(
            page=_Page([]),
            debug={"final_url": "https://example.com/page-2"},
        )
    )
    task = asyncio.create_task(
        paginate_target(
            page=_Page([_Element("/page-2")]),
            target=target,
            result=TargetResult(url=target.url, debug={"final_url": target.url}),
            fetch_target_page=fetch_page,
            extract_page_data=MagicMock(return_value=[]),
            retry_handler=MagicMock(),
            fetcher_cls=object(),
            adaptive=False,
            retryable_status=set(),
            adaptive_dir="/tmp/adaptive",
            proxy_url=None,
            artifacts_dir=None,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 0.2)
        event_loop_progress = asyncio.Event()
        asyncio.get_running_loop().call_soon(event_loop_progress.set)
        await asyncio.wait_for(event_loop_progress.wait(), timeout=0.1)
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=1)
    fetch_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_pagination_resolver_error_defers_to_retryable_fetch(monkeypatch):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )

    def failed_resolver(*_args, **_kwargs):
        raise socket.gaierror("resolver unavailable")

    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        failed_resolver,
    )
    fetch_page = AsyncMock(side_effect=URLResolutionError("resolver unavailable"))

    with pytest.raises(URLResolutionError, match="resolver unavailable"):
        await paginate_target(
            page=_Page([_Element("https://unresolved.example.test/page-2")]),
            target=target,
            result=TargetResult(url=target.url, debug={"final_url": target.url}),
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

    fetch_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_pagination_dns_lookup_obeys_run_deadline(monkeypatch):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    release = threading.Event()

    def blocking_resolver(*_args, **_kwargs):
        release.wait(timeout=1)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 0),
            )
        ]

    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        blocking_resolver,
    )
    budget = RunBudget(
        max_duration_seconds=0.01,
        max_fetched_bytes=1000,
        max_extracted_records=10,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )
    try:
        with pytest.raises(BudgetExceeded) as exc_info:
            await asyncio.wait_for(
                paginate_target(
                    page=_Page([_Element("/page-2")]),
                    target=target,
                    result=TargetResult(
                        url=target.url,
                        debug={"final_url": target.url},
                    ),
                    fetch_target_page=AsyncMock(),
                    extract_page_data=MagicMock(),
                    retry_handler=MagicMock(),
                    fetcher_cls=object(),
                    adaptive=False,
                    retryable_status=set(),
                    adaptive_dir="/tmp/adaptive",
                    proxy_url=None,
                    artifacts_dir=None,
                    budget=budget,
                ),
                timeout=0.2,
            )
    finally:
        release.set()

    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds


@pytest.mark.asyncio
async def test_pagination_dns_lookup_is_cancellable(monkeypatch):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_resolver(*_args, **_kwargs):
        started.set()
        release.wait(timeout=1)
        return []

    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        blocking_resolver,
    )
    task = asyncio.create_task(
        paginate_target(
            page=_Page([_Element("/page-2")]),
            target=target,
            result=TargetResult(url=target.url, debug={"final_url": target.url}),
            fetch_target_page=AsyncMock(),
            extract_page_data=MagicMock(),
            retry_handler=MagicMock(),
            fetcher_cls=object(),
            adaptive=False,
            retryable_status=set(),
            adaptive_dir="/tmp/adaptive",
            proxy_url=None,
            artifacts_dir=None,
        )
    )
    assert await asyncio.to_thread(started.wait, 0.2)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
