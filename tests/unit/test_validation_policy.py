from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.config.schema import OnEmptyAction
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ActionTaken, ErrorType
from scrapeyard.queue.validation_policy import apply_validation
from tests.unit.worker_helpers import make_target


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

    validated = await apply_validation(
        target_cfg=target,
        domain="example.com",
        adaptive=False,
        result=result,
        pending_errors=pending_errors,
        config=MagicMock(project="test", retry=MagicMock()),
        adaptive_dir="/tmp/adaptive",
        run_artifacts_dir="/tmp/artifacts",
        job_id="job-1",
        run_id="run-1",
        circuit_breaker=MagicMock(),
        validator=validator,
        scrape=AsyncMock(),
    )

    assert validated is result
    assert result.errors == ["no rows found"]
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

    validated = await apply_validation(
        target_cfg=target,
        domain="example.com",
        adaptive=True,
        result=first,
        pending_errors=pending_errors,
        config=MagicMock(project="test", retry=MagicMock()),
        adaptive_dir="/tmp/adaptive",
        run_artifacts_dir="/tmp/artifacts",
        job_id="job-1",
        run_id="run-1",
        circuit_breaker=circuit_breaker,
        validator=validator,
        scrape=scrape,
        proxy_url="http://proxy.internal:8080",
    )

    assert validated.status == "failed"
    assert validated.error_type == ErrorType.selector_miss
    assert validated.errors == ["still empty"]
    assert scrape.await_count == 1
    circuit_breaker.record_success.assert_called_once_with("example.com")
    assert [record.action_taken for record in pending_errors] == [ActionTaken.retry, ActionTaken.fail]
