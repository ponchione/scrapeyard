"""Tests for _determine_final_status — pure, no side effects."""

from unittest.mock import MagicMock

from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker import _determine_final_status


def _make_config(strategy: FailStrategy) -> MagicMock:
    cfg = MagicMock()
    cfg.execution.fail_strategy = strategy
    return cfg


def test_all_or_nothing_does_not_mutate_flat_data():
    """G2: _determine_final_status must NOT clear flat_data as a side effect."""
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    flat_data = [{"x": 1}]
    status = _determine_final_status(
        _make_config(FailStrategy.all_or_nothing), results, flat_data,
    )
    assert status == JobStatus.failed
    # flat_data must be UNCHANGED — caller is responsible for clearing.
    assert flat_data == [{"x": 1}]


def test_all_or_nothing_complete_on_all_success():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
    ]
    flat_data = [{"x": 1}]
    status = _determine_final_status(
        _make_config(FailStrategy.all_or_nothing), results, flat_data,
    )
    assert status == JobStatus.complete


def test_partial_returns_partial_on_mixed():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    status = _determine_final_status(
        _make_config(FailStrategy.partial), results, [{"x": 1}],
    )
    assert status == JobStatus.partial


def test_partial_returns_failed_when_all_fail():
    results = [
        TargetResult(url="http://a.com", status="failed", errors=["err"]),
    ]
    status = _determine_final_status(
        _make_config(FailStrategy.partial), results, [],
    )
    assert status == JobStatus.failed


def test_continue_completes_if_data_exists():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    status = _determine_final_status(
        _make_config(FailStrategy.continue_), results, [{"x": 1}],
    )
    assert status == JobStatus.complete


def test_continue_fails_if_no_data():
    results = [
        TargetResult(url="http://a.com", status="failed", errors=["err"]),
    ]
    status = _determine_final_status(
        _make_config(FailStrategy.continue_), results, [],
    )
    assert status == JobStatus.failed
