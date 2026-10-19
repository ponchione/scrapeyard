"""Record/replay page cache and domain-guard behavior on the real fetch path."""

from __future__ import annotations

from pathlib import Path

import pytest
from scrapling import Fetcher
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.api.transport_policy import enforce_submission_page_cache_policy
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import (
    ExecutionConfig,
    FetcherType,
    PageCacheMode,
    RetryConfig,
    TargetConfig,
)
from scrapeyard.engine import pagination
from scrapeyard.engine.domain_guard import (
    LocalDomainGuard,
    RunDomainPolicy,
    activate_domain_policy,
)
from scrapeyard.engine.page_cache import (
    PageCache,
    PageCacheMiss,
    PageCacheUnavailable,
    activate_page_cache,
    canonical_cache_url,
)
from scrapeyard.engine.resilience import RetryableError
from scrapeyard.engine.scrape_models import (
    CLICK_PAGINATION_ATTRIBUTE,
    ClickPaginationResult,
    ClickSnapshot,
)
from scrapeyard.engine.scraper import (
    _fetch_page,
    _record_fetched_page,
    _replay_cached_page,
    scrape_target,
)
from scrapeyard.engine.url_guard import (
    ResolvedPublicURL,
    activate_deployment_secret_redaction,
    reset_deployment_secret_redaction,
)
from scrapeyard.models.job import ErrorType

BASE = "https://shop.example.test/list"
_REAL_PAGINATION_URL_CHECK = pagination._pagination_url_is_safe


def _page_html(items: list[str], next_href: str | None) -> str:
    cards = "".join(f'<div class="card"><h2>{item}</h2></div>' for item in items)
    link = f'<a class="next" href="{next_href}">Next</a>' if next_href else ""
    return f"<html><head><title>List</title></head><body>{cards}{link}</body></html>"


PAGES = {
    BASE: _page_html(["A", "B"], "/list?page=2"),
    f"{BASE}?page=2": _page_html(["C"], "/list?page=3"),
    f"{BASE}?page=3": _page_html(["D"], None),
}


def _response(url: str, html: str, status: int = 200) -> Response:
    return Response(
        url=url,
        text=html,
        body=html.encode(),
        status=status,
        reason="OK",
        cookies={},
        headers={"Content-Type": "text/html; charset=utf-8"},
        request_headers={},
        **Fetcher._generate_parser_arguments(),
    )


def _target(**pagination) -> TargetConfig:
    return TargetConfig(
        url=BASE,
        fetcher=FetcherType.basic,
        item_selector=".card",
        selectors={"name": "h2::text"},
        pagination=pagination or {"next": "a.next", "max_pages": 5},
    )


class _Network:
    """Fake site for the basic fetch path; counts every request."""

    def __init__(self, *, fail_first: int = 0) -> None:
        self.requests: list[str] = []
        self.fail_first = fail_first

    async def __call__(self, _fetcher_cls, url, _call_kwargs, **_kwargs):
        self.requests.append(url)
        if self.fail_first:
            self.fail_first -= 1
            raise RetryableError(503)
        return _response(url, PAGES[url])


def _no_network(*_args, **_kwargs):
    raise AssertionError("replay must not make a request")


async def _allow(*_args, **_kwargs) -> bool:
    return True


@pytest.fixture(autouse=True)
def _public_test_site(monkeypatch):
    """Treat example.test as public without DNS; the fake network serves it."""
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.resolve_public_url",
        lambda url: ResolvedPublicURL(url, "shop.example.test", "shop.example.test"),
    )
    monkeypatch.setattr("scrapeyard.engine.pagination._pagination_url_is_safe", _allow)


def _names(result) -> list[str]:
    return [item["name"] for item in result.data]


def _cache(tmp_path: Path, mode: PageCacheMode) -> PageCache:
    return PageCache.for_project(mode, str(tmp_path / "cache"), "demo")


async def _scrape(target: TargetConfig, tmp_path: Path, **retry):
    return await scrape_target(
        target,
        adaptive=False,
        retry=RetryConfig(backoff_max=0, **retry),
        adaptive_dir=str(tmp_path / "adaptive"),
    )


def test_cache_entries_are_keyed_by_canonical_url_fetcher_and_click_page(tmp_path):
    cache = _cache(tmp_path, PageCacheMode.record)
    cache.store(f"{BASE}#frag", "basic", html="<p>1</p>", final_url=BASE, status=200,
                content_type="text/html")
    cache.store(BASE, "dynamic", html="<p>2</p>", final_url=BASE, status=200,
                content_type="text/html")
    cache.store(BASE, "basic", html="<p>3</p>", final_url=BASE, status=200,
                content_type="text/html", click_index=1)

    assert canonical_cache_url("HTTPS://Shop.Example.test/list#x") == BASE
    assert cache.load("https://SHOP.example.test/list", "basic").html == "<p>1</p>"
    assert cache.load(BASE, "dynamic").html == "<p>2</p>"
    assert cache.load(BASE, "basic", click_index=1).html == "<p>3</p>"
    assert cache.load(BASE, "stealthy") is None
    assert PageCache.for_project(PageCacheMode.replay, str(tmp_path / "cache"), "other").load(
        BASE, "basic"
    ) is None
    assert not list((tmp_path / "cache").rglob(".*"))


@pytest.mark.asyncio
async def test_record_then_replay_reproduces_results_without_network(tmp_path, monkeypatch):
    network = _Network()
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", network)
    with activate_page_cache(_cache(tmp_path, PageCacheMode.record)):
        recorded = await _scrape(_target(), tmp_path)
    assert recorded.is_success and len(network.requests) == 3
    assert recorded.debug["page_cache"] == {"mode": "record"}
    assert recorded.recorded_at is None

    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _no_network)
    # Replay skips the pagination DNS check itself; any resolution would fail here.
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", _REAL_PAGINATION_URL_CHECK)
    monkeypatch.setattr("scrapeyard.engine.pagination.assert_public_url", _no_network)
    with activate_page_cache(_cache(tmp_path, PageCacheMode.replay)):
        replayed = await _scrape(_target(), tmp_path)

    assert replayed.is_success
    assert replayed.data == recorded.data
    assert _names(replayed) == ["A", "B", "C", "D"]
    assert replayed.pagination_stop_reason == "exhausted"
    assert replayed.debug["page_cache"]["mode"] == "replay"
    assert replayed.recorded_at and replayed.recorded_at == replayed.debug["page_cache"]["recorded_at"]


@pytest.mark.asyncio
async def test_a_cache_that_cannot_be_written_does_not_fail_the_fetched_pages(
    tmp_path, monkeypatch
):
    network = _Network()
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", network)
    (tmp_path / "cache").write_text("not a directory")

    with activate_page_cache(_cache(tmp_path, PageCacheMode.record)):
        result = await _scrape(_target(), tmp_path)

    # It failed the target as network_error, which counts against the host's
    # circuit breaker, after the site had answered.
    assert result.is_success and result.error_type is None
    assert _names(result) == ["A", "B", "C", "D"]
    assert len(network.requests) == 3
    assert result.debug["page_cache"] == {"mode": "record", "stored": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "html"),
    [
        (f"{BASE}?key=tok-9f1c2e7d5a", _page_html(["A"], None)),
        (BASE, _page_html(["A"], None).replace("</body>", "<p>tok-9f1c2e7d5a</p></body>")),
    ],
    ids=["in-the-url", "in-the-page"],
)
async def test_record_writes_nothing_that_holds_a_resolved_secret(tmp_path, monkeypatch, url, html):
    async def site(_fetcher_cls, requested, _call_kwargs, **_kwargs):
        return _response(requested, html)

    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", site)
    token = activate_deployment_secret_redaction(("tok-9f1c2e7d5a",))
    try:
        with activate_page_cache(_cache(tmp_path, PageCacheMode.record)):
            result = await _scrape(_target().model_copy(update={"url": url}), tmp_path)
    finally:
        reset_deployment_secret_redaction(token)

    assert result.is_success and _names(result) == ["A"]
    assert result.debug["page_cache"] == {"mode": "record", "stored": False}
    written = [path.read_text() for path in (tmp_path / "cache").rglob("*") if path.is_file()]
    assert not any("tok-9f1c2e7d5a" in text for text in written)


@pytest.mark.parametrize(
    ("secret", "page"),
    [
        # httpx keeps "/" but escapes "<" and ">", so neither form matched.
        ("tok/en<1>", {"final_url": f"{BASE}?key=tok/en%3C1%3E"}),
        ("tok-9f1c2e7d5a", {"content_type": "text/html; tok-9f1c2e7d5a"}),
        ("AbCdEf0123456789", {"final_url": "https://abcdef0123456789.example.test/list"}),
        ("tok<9f1c2e7d5a", {"html": "<p>tok&lt;9f1c2e7d5a</p>"}),
        ("tok-9f1c2e7d5a", {"click_pagination": {"pages": 2, "next": "tok-9f1c2e7d5a"}}),
        # A login redirect carrying the URL httpx sent, encoded once more.
        ("tok/en<1>", {"final_url": f"{BASE}?next=%2Flist%3Fkey%3Dtok%2Fen%253C1%253E"}),
        ("tok en/1", {"html": '<a href="/p?key=tok+en%2F1">next</a>'}),
        ("AbC/dEf+GhI0123=", {"html": '<script>var t = "AbC\\/dEf+GhI0123=";</script>'}),
        ("tok/en<1>", {"html": '<script>var t = "tok/en\\u003c1>";</script>'}),
        ("tok/en<1>", {"html": '<a href="/p?key=tok&#37;2Fen&#37;3C1&#37;3E">next</a>'}),
    ],
    ids=[
        "partly-escaped", "content-type", "lowercased-host", "html-entity", "click-snapshot",
        "escaped-twice", "form-encoded", "json-slash", "json-unicode", "entity-percent",
    ],
)
def test_no_reading_of_a_resolved_secret_is_written(tmp_path, secret, page):
    cache = _cache(tmp_path, PageCacheMode.record)
    token = activate_deployment_secret_redaction((secret,))
    try:
        stored = cache.store(BASE, "basic", **{
            "html": "<p>1</p>", "final_url": BASE, "status": 200, "content_type": "text/html",
            **page,
        })
    finally:
        reset_deployment_secret_redaction(token)

    assert stored is False
    assert not [path for path in (tmp_path / "cache").rglob("*") if path.is_file()]


def test_recorded_final_url_keeps_no_userinfo(tmp_path):
    cache = _cache(tmp_path, PageCacheMode.record)

    assert cache.store(BASE, "basic", html="<p>1</p>", final_url="https://user:S3CRET@shop.example.test/list",
                       status=200, content_type="text/html")

    assert cache.load(BASE, "basic").final_url == BASE
    assert not any("S3CRET" in path.read_text() for path in (tmp_path / "cache").rglob("*.json"))


@pytest.mark.asyncio
async def test_replay_miss_fails_first_page_and_stops_pagination_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _no_network)
    cache = _cache(tmp_path, PageCacheMode.replay)
    with activate_page_cache(cache):
        missing = await _scrape(_target(), tmp_path)
    assert missing.error_type is ErrorType.cache_miss
    assert missing.http_status is None

    cache.store(BASE, "basic", html=PAGES[BASE], final_url=BASE, status=200,
                content_type="text/html")
    with activate_page_cache(cache):
        partial = await _scrape(_target(), tmp_path)
    assert partial.is_success
    assert _names(partial) == ["A", "B"]
    assert partial.pagination_stop_reason == "cache_miss"
    assert any("Page cache miss" in error for error in partial.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_href", ["https://straße.test/list?page=2", "https://a..b/list"])
async def test_replay_stops_at_a_refused_next_link_as_the_live_run_does(
    tmp_path, monkeypatch, next_href
):
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _no_network)
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", _REAL_PAGINATION_URL_CHECK)
    monkeypatch.setattr("scrapeyard.engine.pagination.assert_public_url", _no_network)
    cache = _cache(tmp_path, PageCacheMode.replay)
    cache.store(BASE, "basic", html=_page_html(["A", "B"], next_href), final_url=BASE,
                status=200, content_type="text/html")

    with activate_page_cache(cache):
        result = await _scrape(_target(), tmp_path)

    assert result.is_success
    assert _names(result) == ["A", "B"]
    assert result.pagination_stop_reason == "unsafe_next_url"


@pytest.mark.asyncio
async def test_unconfigured_cache_fails_instead_of_fetching(tmp_path, monkeypatch):
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _no_network)
    with activate_page_cache(None, unavailable=True):
        result = await _scrape(_target(), tmp_path)
    assert result.error_type is ErrorType.cache_miss
    assert "SCRAPEYARD_PAGE_CACHE_DIR" in (result.error_detail or "")
    with activate_page_cache(None, unavailable=True), pytest.raises(PageCacheUnavailable):
        await _fetch_page(object(), BASE, _target(), FetcherType.basic, False, {500}, str(tmp_path))


def _click_snapshot(url: str, html: str) -> ClickSnapshot:
    return ClickSnapshot.capture(html, {
        "url": url,
        "status": 200,
        "reason": "OK",
        "cookies": {},
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "request_headers": {},
        **Fetcher._generate_parser_arguments(),
    })


@pytest.mark.asyncio
async def test_click_snapshots_are_recorded_and_replayed(tmp_path):
    cache = _cache(tmp_path, PageCacheMode.record)
    first = _response(BASE, PAGES[BASE])
    setattr(
        first,
        CLICK_PAGINATION_ATTRIBUTE,
        ClickPaginationResult(
            pages=[
                _click_snapshot(BASE, PAGES[f"{BASE}?page=2"]),
                _click_snapshot(BASE, PAGES[f"{BASE}?page=3"]),
            ],
            stop_reason="exhausted",
        ),
    )
    _record_fetched_page(cache, BASE, FetcherType.dynamic, first, BASE)
    # Snapshots are stored as serialized by the parser, like every other page.
    assert cache.load(BASE, "dynamic", click_index=2).html == (
        _response(BASE, PAGES[f"{BASE}?page=3"]).html_content
    )

    target = TargetConfig(
        url=BASE,
        fetcher=FetcherType.dynamic,
        item_selector=".card",
        selectors={"name": "h2::text"},
        pagination={"mode": "click", "next": "a.next", "max_pages": 5},
    )
    replay = PageCache(mode=PageCacheMode.replay, root=cache.root)
    outcome = await _replay_cached_page(
        replay, BASE, target, FetcherType.dynamic,
        adaptive=False, adaptive_dir=str(tmp_path), budget=None,
    )
    click = getattr(outcome.page, CLICK_PAGINATION_ATTRIBUTE)
    assert click.stop_reason == "exhausted"
    assert all(isinstance(page, ClickSnapshot) for page in click.pages)
    names = []
    for snapshot in click.pages:
        with snapshot.parsed() as page:
            names.append(page.css("h2::text"))
    assert names == [["C"], ["D"]]

    for path in cache.root.rglob("*.json"):
        if '"click_index": 2' in path.read_text():
            path.unlink()
    truncated = await _replay_cached_page(
        replay, BASE, target, FetcherType.dynamic,
        adaptive=False, adaptive_dir=str(tmp_path), budget=None,
    )
    click = getattr(truncated.page, CLICK_PAGINATION_ATTRIBUTE)
    assert (len(click.pages), click.stop_reason) == (1, "cache_miss")
    assert "click page 3" in click.stop_detail


def test_page_cache_mode_requires_a_service_directory():
    config = load_config(
        "project: demo\nname: cache\nexecution:\n  page_cache: replay\n"
        "target:\n  url: https://shop.example.test/\n  selectors:\n    t: h1\n"
    )

    class _Settings:
        page_cache_dir = ""

    with pytest.raises(ValueError, match="SCRAPEYARD_PAGE_CACHE_DIR"):
        enforce_submission_page_cache_policy(config, settings=_Settings())  # type: ignore[arg-type]
    _Settings.page_cache_dir = "/cache"
    enforce_submission_page_cache_policy(config, settings=_Settings())  # type: ignore[arg-type]
    assert ExecutionConfig().page_cache is PageCacheMode.off
    with pytest.raises(ValueError):
        ExecutionConfig(domain_daily_page_limit=0)


# --- domain guard on the fetch path -------------------------------------------


def _policy(**kwargs) -> RunDomainPolicy:
    return RunDomainPolicy(guard=LocalDomainGuard(), **kwargs)


@pytest.mark.asyncio
async def test_cooling_host_is_not_contacted(tmp_path, monkeypatch):
    network = _Network()
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", network)
    policy = _policy(daily_page_limit=0, cooldown_seconds=600)
    await policy.record_denial(BASE)
    with activate_domain_policy(policy):
        result = await _scrape(_target(), tmp_path)
    assert result.error_type is ErrorType.domain_cooldown
    assert network.requests == []
    assert "no request was made" in (result.error_detail or "")


@pytest.mark.asyncio
async def test_daily_limit_counts_retries_and_stops_pagination_with_pages_kept(tmp_path, monkeypatch):
    network = _Network(fail_first=1)
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", network)
    policy = _policy(daily_page_limit=3, cooldown_seconds=0)
    with activate_domain_policy(policy):
        result = await _scrape(_target(), tmp_path, max_attempts=2)

    # Attempts: page 1 (503, then retry), page 2; page 3 is refused before a request.
    assert network.requests == [BASE, BASE, f"{BASE}?page=2"]
    assert result.is_success
    assert _names(result) == ["A", "B", "C"]
    assert result.pagination_stop_reason == "domain_guard"
    assert any("daily page limit 3" in error for error in result.errors)
    assert policy.snapshot()["daily_limit_stops"] == {"shop.example.test": 1}


@pytest.mark.asyncio
async def test_replay_bypasses_the_domain_guard(tmp_path, monkeypatch):
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _no_network)
    cache = _cache(tmp_path, PageCacheMode.replay)
    for url, html in PAGES.items():
        cache.store(url, "basic", html=html, final_url=url, status=200, content_type="text/html")
    policy = _policy(daily_page_limit=1, cooldown_seconds=600)
    await policy.record_denial(BASE)
    with activate_domain_policy(policy), activate_page_cache(cache):
        result = await _scrape(_target(), tmp_path)
    assert result.is_success and len(result.data) == 4
    assert policy.usage.pages_admitted == {}


def test_miss_messages_identify_the_page():
    assert "click page 2" in str(PageCacheMiss(BASE, click_index=1))
