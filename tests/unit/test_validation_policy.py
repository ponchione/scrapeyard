from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.config.schema import OnEmptyAction
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ActionTaken, ErrorType
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.queue.validation_policy import apply_validation
from tests.unit.worker_helpers import make_target


def _recorder(pending_errors, circuit_breaker=None) -> TargetErrorRecorder:
    return TargetErrorRecorder(
        job_id="job-1",
        run_id="run-1",
        project="test",
        pending_errors=pending_errors,
        circuit_breaker=circuit_breaker or MagicMock(),
    )


def _config(domain_rate_limit: int = 0) -> MagicMock:
    config = MagicMock(project="test", retry=MagicMock())
    config.execution.domain_rate_limit = domain_rate_limit
    return config


@pytest.mark.asyncio
async def test_apply_validation_warn_appends_warning_and_returns_original_result():
    target = make_target("https://example.com")
    result = TargetResult(url=target.url, status="success", data=[], errors=[])
    validator = MagicMock()
    validator.validate.return_value = MagicMock(
        passed=False,
        action=OnEmptyAction.warn,
        message="no rows found",
    )
    pending_errors = []
    rate_limiter = AsyncMock()

    validated = await apply_validation(
        target_cfg=target,
        domain="example.com",
        adaptive=False,
        result=result,
        config=_config(),
        adaptive_dir="/tmp/adaptive",
        run_artifacts_dir="/tmp/artifacts",
        recorder=_recorder(pending_errors),
        rate_limiter=rate_limiter,
        validator=validator,
        scrape=AsyncMock(),
    )

    assert validated is result
    assert result.errors == ["no rows found"]
    rate_limiter.acquire.assert_not_awaited()
    assert len(pending_errors) == 1
    assert pending_errors[0].action_taken == ActionTaken.warn
    assert pending_errors[0].error_type == ErrorType.content_empty


@pytest.mark.asyncio
async def test_apply_validation_retry_returns_failed_result_after_second_invalid_response():
    target = make_target("https://example.com")
    first = TargetResult(url=target.url, status="success", data=[], errors=[])
    retried = TargetResult(url=target.url, status="success", data=[], errors=[], debug={"classification": "selector_miss"})
    validator = MagicMock()
    validator.validate.side_effect = [
        MagicMock(passed=False, action=OnEmptyAction.retry, message="empty first pass"),
        MagicMock(passed=False, action=OnEmptyAction.retry, message="still empty"),
    ]
    scrape = AsyncMock(return_value=retried)
    pending_errors = []
    circuit_breaker = MagicMock()
    rate_limiter = AsyncMock()

    validated = await apply_validation(
        target_cfg=target,
        domain="example.com",
        adaptive=True,
        result=first,
        config=_config(domain_rate_limit=4),
        adaptive_dir="/tmp/adaptive",
        run_artifacts_dir="/tmp/artifacts",
        recorder=_recorder(pending_errors, circuit_breaker),
        rate_limiter=rate_limiter,
        validator=validator,
        scrape=scrape,
        proxy_url="http://proxy.internal:8080",
    )

    assert validated.status == "failed"
    assert validated.error_type == ErrorType.selector_miss
    assert validated.errors == ["still empty"]
    rate_limiter.acquire.assert_awaited_once_with("example.com", 4)
    assert scrape.await_count == 1
    circuit_breaker.record_success.assert_called_once_with("example.com")
    assert [record.action_taken for record in pending_errors] == [ActionTaken.retry, ActionTaken.fail]
