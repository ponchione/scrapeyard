"""Runs share domain cooldowns/budgets and report page-cache replay outcomes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scrapling import Fetcher
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.common.settings import ServiceSettings
from scrapeyard.config.schema import PageCacheMode
from scrapeyard.engine.domain_guard import LocalDomainGuard
from scrapeyard.engine.page_cache import PageCache
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.url_guard import ResolvedPublicURL
from scrapeyard.models.job import ActionTaken, ErrorType, JobStatus
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.types import SaveResultMeta
from tests.unit.worker_helpers import make_job

URL = "https://shop.example.test/item"
CONFIG = f"""
project: demo
name: guard-job
target:
  url: {URL}
  fetcher: basic
  selectors:
    title: h1
execution:
  domain_daily_page_limit: 5
"""


def _response(status: int, html: str, url: str = URL) -> Response:
    return Response(
        url=url,
        text=html,
        body=html.encode(),
        status=status,
        reason="",
        cookies={},
        headers={"Content-Type": "text/html"},
        request_headers={},
        **Fetcher._generate_parser_arguments(),
    )


class _Site:
    def __init__(self, status: int) -> None:
        self.status = status
        self.requests: list[str] = []

    async def __call__(self, _fetcher_cls, url, _call_kwargs, **_kwargs):
        self.requests.append(url)
        return _response(self.status, "<html><body><h1>Title</h1></body></html>")


def _settings(tmp_path, **overrides) -> ServiceSettings:
    return ServiceSettings(
        db_dir=str(tmp_path / "db"),
        storage_results_dir=str(tmp_path / "results"),
        adaptive_dir=str(tmp_path / "adaptive"),
        log_dir=str(tmp_path / "logs"),
        domain_denial_cooldown_seconds=600,
        **overrides,
    )


async def _run(tmp_path, config: str, site: _Site, guard, run_id: str, **settings):
    job_store = AsyncMock()
    job_store.get_job.return_value = make_job(
        job_id="job-guard", name="guard-job", status=JobStatus.queued, current_run_id=run_id,
    )
    job_store.claim_run.return_value = True
    result_store = AsyncMock()
    result_store.save_result.return_value = SaveResultMeta(
        run_id=run_id, file_path="/tmp/result", record_count=0, serialized_bytes=256,
    )
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0
    circuit_breaker = MagicMock()
    with (
        patch("scrapeyard.queue.worker.get_settings", return_value=_settings(tmp_path, **settings)),
        patch("scrapeyard.engine.scraper.fetch_basic_response", site),
        patch(
            "scrapeyard.engine.scraper.resolve_public_url",
            lambda url: ResolvedPublicURL(url, "shop.example.test", "shop.example.test"),
        ),
    ):
        await scrape_task(
            "job-guard",
            config,
            run_id=run_id,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            domain_guard=guard,
        )
    output = result_store.save_result.await_args.args[1]
    errors = [
        record
        for call in error_store.log_errors.await_args_list
        for record in call.args[0]
    ]
    return output, errors, circuit_breaker


@pytest.mark.asyncio
async def test_denial_starts_a_cooldown_that_later_runs_honor_without_requests(tmp_path):
    guard = LocalDomainGuard()
    blocked = _Site(403)
    first, _errors, _breaker = await _run(tmp_path, CONFIG, blocked, guard, "run-1")
    assert blocked.requests == [URL]
    assert first["run_budget"]["domain_guard"]["cooldowns_started"] == ["shop.example.test"]
    assert first["run_budget"]["domain_guard"]["pages_admitted"] == {"shop.example.test": 1}
    assert (await guard.status("shop.example.test")).cooldown_remaining_seconds > 0

    healthy = _Site(200)
    second, errors, breaker = await _run(tmp_path, CONFIG, healthy, guard, "run-2")
    assert healthy.requests == []
    assert "domain_cooldown" in str(second["results"])
    assert second["run_budget"]["domain_guard"]["cooldown_stops"] == {"shop.example.test": 1}
    assert [(e.error_type, e.action_taken) for e in errors] == [
        (ErrorType.domain_cooldown, ActionTaken.skip)
    ]
    breaker.record_failure.assert_not_called()
    assert "page_cache" not in second


@pytest.mark.asyncio
async def test_job_limit_stops_later_runs_and_is_reported(tmp_path):
    guard = LocalDomainGuard()
    for _ in range(5):
        await guard.consume_page("shop.example.test", 0)
    site = _Site(200)
    output, errors, _breaker = await _run(tmp_path, CONFIG, site, guard, "run-limit")
    assert site.requests == []
    assert output["run_budget"]["domain_guard"]["daily_page_limit"] == 5
    assert output["run_budget"]["domain_guard"]["daily_limit_stops"] == {"shop.example.test": 1}
    assert [e.error_type for e in errors] == [ErrorType.domain_daily_limit]


@pytest.mark.asyncio
async def test_replay_run_is_labeled_and_never_counts_or_contacts_the_site(tmp_path):
    cache_dir = tmp_path / "page-cache"
    PageCache.for_project(PageCacheMode.record, str(cache_dir), "demo").store(
        URL, "basic", html="<html><body><h1>Recorded</h1></body></html>",
        final_url=URL, status=200, content_type="text/html",
    )
    guard = LocalDomainGuard()
    await guard.start_cooldown("shop.example.test", 600)
    site = _Site(200)
    output, errors, breaker = await _run(
        tmp_path,
        CONFIG + "  page_cache: replay\n",
        site,
        guard,
        "run-replay",
        page_cache_dir=str(cache_dir),
    )
    assert site.requests == []
    assert errors == []
    assert output["page_cache"] == "replay"
    assert "Recorded" in str(output["results"])
    assert "recorded_at" in str(output["results"])
    assert output["run_budget"]["domain_guard"]["pages_admitted"] == {}
    breaker.record_success.assert_not_called()


SECOND_URL = "https://shop.example.test/second"
# Every MidwayUSA page embeds Akamai's bot-manager script, whose text contains
# challenge markers ("akamai", "sec-if-cpt-container") on ordinary listings.
AKAMAI_SCRIPT = (
    '<script src="/akam/13/7d1c">/* Akamai bot manager */ '
    "var container = 'sec-if-cpt-container';</script>"
)


def _listing(*names: str) -> str:
    cards = "".join(
        f'<li class="product"><h2>{name}</h2><span class="price">$10.00</span></li>'
        for name in names
    )
    return (
        f"<html><head><title>Scopes | Shop</title>{AKAMAI_SCRIPT}</head>"
        f"<body><ol>{cards}</ol></body></html>"
    )


CHALLENGE = (
    f"<html><head><title>Access Denied</title>{AKAMAI_SCRIPT}</head>"
    '<body><div id="sec-if-cpt-container">Press &amp; Hold to confirm you are a human</div>'
    "</body></html>"
)


def _listing_config(on_empty: str) -> str:
    target = """
  - url: {url}
    fetcher: basic
    item_selector: li.product
    selectors:
      name: h2
      price: .price"""
    return f"""
project: demo
name: guard-job
targets:{target.format(url=URL)}{target.format(url=SECOND_URL)}
execution:
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
validation:
  required_fields: [name, price]
  min_results: 1
  on_empty: {on_empty}
"""


class _Pages:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    async def __call__(self, _fetcher_cls, url, _call_kwargs, **_kwargs):
        self.requests.append(url)
        return _response(200, self.pages[url], url)


@pytest.mark.asyncio
@pytest.mark.parametrize("on_empty", ["warn", "fail"])
async def test_validation_failure_on_a_rendered_listing_is_not_an_access_denial(tmp_path, on_empty):
    """Midway 2026-10-09: one blank card name on a normal page that carries
    Akamai's script must stay a validation outcome, not a host-wide challenge."""
    guard = LocalDomainGuard()
    site = _Pages({URL: _listing("Scope A", "", "Scope C"), SECOND_URL: _listing("Scope D")})
    output, errors, _breaker = await _run(
        tmp_path, _listing_config(on_empty), site, guard, "run-validation",
    )

    assert site.requests == [URL, SECOND_URL]
    assert output["run_budget"]["domain_guard"]["cooldowns_started"] == []
    assert (await guard.status("shop.example.test")).cooldown_remaining_seconds == 0
    first = errors[0]
    assert first.error_type == ErrorType.content_empty
    assert first.error_message == "Required field 'name' is empty in record 1"
    assert first.action_taken == ActionTaken(on_empty)
    results = str(output["results"])
    assert "challenge_page" not in results
    assert "Scope D" in results
    if on_empty == "warn":
        assert "Scope A" in results and "Scope C" in results


@pytest.mark.asyncio
async def test_challenge_page_without_items_still_denies_the_host(tmp_path):
    guard = LocalDomainGuard()
    site = _Pages({URL: CHALLENGE, SECOND_URL: _listing("Scope D")})
    output, errors, _breaker = await _run(
        tmp_path, _listing_config("warn"), site, guard, "run-challenge",
    )

    assert site.requests == [URL]
    assert output["run_budget"]["domain_guard"]["cooldowns_started"] == ["shop.example.test"]
    assert (await guard.status("shop.example.test")).cooldown_remaining_seconds > 0
    assert [e.error_type for e in errors] == [ErrorType.challenge_page]
    assert "earlier target on this host was denied or challenged" in str(output["results"])
