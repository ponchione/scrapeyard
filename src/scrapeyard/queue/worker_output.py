"""Result assembly, final status determination, and deployment-secret redaction."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.json_encoding import compact_json_size
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import FailStrategy, GroupBy, PageCacheMode, ScrapeConfig
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.engine.url_guard import (
    redact_deployment_secret_values,
    redact_deployment_secrets_with_count,
    redact_sensitive_mapping,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
    url_host_label,
)
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker_context import JobExecutionContext


def collect_result_payload(
    all_results: list[TargetResult],
) -> tuple[list[dict[str, Any]], list[str]]:
    flat_data: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for target_result in all_results:
        flat_data.extend(target_result.data)
        all_errors.extend(target_result.errors)
    return flat_data, all_errors


def annotate_run_output(
    output_data: dict[str, Any],
    context: JobExecutionContext,
) -> None:
    """Report cache mode and domain-guard outcomes alongside the run budget."""
    if context.domain_policy is not None and isinstance(output_data.get("run_budget"), dict):
        output_data["run_budget"]["domain_guard"] = context.domain_policy.snapshot()
    if context.config.execution.page_cache is not PageCacheMode.off:
        output_data["page_cache"] = context.config.execution.page_cache.value


def determine_final_status(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    flat_data: list[dict[str, Any]],
) -> JobStatus:
    """Determine the final job status based on results and fail_strategy."""
    failed_count = sum(1 for result in all_results if result.is_failed)
    fail_strategy = config.execution.fail_strategy

    if fail_strategy == FailStrategy.all_or_nothing:
        if failed_count > 0:
            return JobStatus.failed
        return JobStatus.complete
    if fail_strategy == FailStrategy.continue_:
        return JobStatus.complete if flat_data else JobStatus.failed
    if failed_count == len(all_results) or not flat_data:
        return JobStatus.failed
    if failed_count > 0:
        return JobStatus.partial
    return JobStatus.complete


def _target_result_details(
    result: TargetResult,
    *,
    records_accepted: bool = True,
) -> dict[str, Any]:
    debug = redact_sensitive_mapping(result.debug) if result.debug is not None else None
    if isinstance(debug, dict):
        debug["screenshot_path"] = None
    return {
        "status": result.status_value,
        "count": len(result.data) if records_accepted else 0,
        "observed_count": len(result.data),
        "pagination": {
            "stop_reason": result.pagination_stop_reason,
            "exhausted": result.pagination_stop_reason == "exhausted",
        },
        **({"recorded_at": result.recorded_at} if result.recorded_at else {}),
        "debug": debug,
        "error_type": result.error_type.value if result.error_type else None,
        "error_detail": (
            redact_userinfo_in_text(result.error_detail)
            if result.error_detail is not None
            else None
        ),
    }


def _unique_result_group_key(grouped: dict[str, Any], url: str) -> str:
    base_key = url_host_label(url)
    if base_key not in grouped:
        return base_key
    index = 2
    while f"{base_key}#{index}" in grouped:
        index += 1
    return f"{base_key}#{index}"


def format_output(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    job_id: str,
    final_status: JobStatus,
    all_errors: list[str],
    *,
    publish_results: bool = True,
    budget: RunBudget | None = None,
    now_fn: Callable[[], datetime] = utc_now,
    size_fn: Callable[[Any], int] = compact_json_size,
) -> dict[str, Any]:
    """Build the output data dict for result storage."""
    redacted_errors: list[str] = []
    for error in all_errors:
        redacted_error = redact_userinfo_in_text(error)
        if budget is not None:
            budget.reserve_estimated_result_bytes(size_fn(redacted_error) + 1)
        redacted_errors.append(redacted_error)

    target_summaries: list[dict[str, Any]] = []
    for result in all_results:
        target_summary = {
            "url": redact_userinfo_in_url(result.url),
            **_target_result_details(
                result,
                records_accepted=publish_results,
            ),
            "pages_scraped": result.pages_scraped,
            "errors": [redact_userinfo_in_text(error) for error in result.errors],
        }
        if budget is not None:
            budget.reserve_estimated_result_bytes(size_fn(target_summary) + 1)
        target_summaries.append(target_summary)

    job_meta: dict[str, Any] = {
        "project": config.project,
        "name": config.name,
        "job_id": job_id,
        "status": final_status.value,
        "completed_at": now_fn().isoformat(),
        "errors": redacted_errors,
        "targets": target_summaries,
    }
    if budget is not None:
        budget.reserve_estimated_result_bytes(
            size_fn({**job_meta, "errors": [], "targets": []})
        )

    if not publish_results:
        empty_results: list[Any] | dict[str, Any]
        empty_results = [] if config.output.group_by == GroupBy.merge else {}
        if budget is not None:
            budget.reserve_estimated_result_bytes(size_fn({"results": empty_results}))
        return {**job_meta, "results": empty_results}

    if config.output.group_by == GroupBy.merge:
        if budget is not None:
            budget.reserve_estimated_result_bytes(size_fn({"results": []}))
        merged: list[Any] = []
        for result in all_results:
            source = url_host_label(result.url)
            source_field_size = size_fn({"_source": source}) - 2
            for item in result.data:
                if isinstance(item, dict):
                    if "_source" in item:
                        raise ValueError(
                            "Extracted record contains reserved merge field '_source'"
                        )
                    if budget is not None:
                        budget.reserve_estimated_result_bytes(
                            source_field_size + int(bool(item))
                        )
                    merged.append({**item, "_source": source})
                else:
                    merged.append(item)
        return {**job_meta, "results": merged}

    grouped: dict[str, Any] = {}
    if budget is not None:
        budget.reserve_estimated_result_bytes(size_fn({"results": {}}))
    for result in all_results:
        group_key = _unique_result_group_key(grouped, result.url)
        target_output = {
            **_target_result_details(result),
            "data": result.data,
        }
        if budget is not None:
            budget.reserve_estimated_result_bytes(
                size_fn({group_key: {**target_output, "data": []}})
            )
        grouped[group_key] = target_output
    return {**job_meta, "results": grouped}


def _redact_output_artifact(
    config: ScrapeConfig,
    output_data: dict[str, Any],
    *,
    records_already_redacted: bool = False,
    record_redaction_count: int = 0,
    budget: RunBudget | None = None,
    size_fn: Callable[[Any], int] = compact_json_size,
) -> dict[str, Any]:
    """Redact diagnostics aggressively without corrupting extracted records."""
    secret_values = config.resolved_secret_values
    results = output_data["results"]
    redacted_output: dict[str, Any] = redact_sensitive_mapping(
        {key: value for key, value in output_data.items() if key != "results"},
        secret_values=secret_values,
    )

    if config.output.group_by == GroupBy.merge:
        if records_already_redacted:
            redacted_results: Any = results
            redaction_count = record_redaction_count
        else:
            redacted_results, redaction_count = redact_deployment_secret_values(
                results,
                secret_values=secret_values,
            )
    else:
        redacted_results, redaction_count = _redact_grouped_results(
            results,
            secret_values=secret_values,
            records_already_redacted=records_already_redacted,
            record_redaction_count=record_redaction_count,
        )
    redacted_output["results"] = redacted_results
    if redaction_count:
        redaction_metadata = {
            "deployment_secret_matches": redaction_count,
        }
        if budget is not None:
            budget.reserve_estimated_result_bytes(
                size_fn({"result_redaction": redaction_metadata})
            )
        redacted_output["result_redaction"] = redaction_metadata
    if budget is not None:
        budget.check_deadline()
    return redacted_output


def _redact_grouped_results(
    results: dict[str, Any],
    *,
    secret_values: Any,
    records_already_redacted: bool,
    record_redaction_count: int,
) -> tuple[dict[str, Any], int]:
    """Redact each target group's diagnostics, records and group key."""
    redacted_results: dict[str, Any] = {}
    redaction_count = record_redaction_count
    for group_key, target_result in results.items():
        data = target_result["data"]
        redacted_target = redact_sensitive_mapping(
            {key: value for key, value in target_result.items() if key != "data"},
            secret_values=secret_values,
        )
        if records_already_redacted:
            redacted_data = data
            data_count = 0
        else:
            redacted_data, data_count = redact_deployment_secret_values(
                data,
                secret_values=secret_values,
            )
        redacted_target["data"] = redacted_data
        redaction_count += data_count
        redacted_group_key, group_key_count = redact_deployment_secrets_with_count(
            group_key,
            secret_values,
        )
        redaction_count += group_key_count
        unique_group_key = redacted_group_key
        suffix = 2
        while unique_group_key in redacted_results:
            unique_group_key = f"{redacted_group_key}#{suffix}"
            suffix += 1
        redacted_results[unique_group_key] = redacted_target
    return redacted_results, redaction_count


def _redact_result_records(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    *,
    budget: RunBudget,
    size_fn: Callable[[Any], int] = compact_json_size,
) -> int:
    """Redact records one at a time without retaining a second full result graph."""

    secret_values = config.resolved_secret_values
    if not secret_values:
        return 0
    redaction_count = 0
    for result in all_results:
        for index, item in enumerate(result.data):
            redacted_item, item_count = redact_deployment_secret_values(
                item,
                secret_values=secret_values,
            )
            # Only a replacement can change a record's serialized size.
            if item_count:
                growth = size_fn(redacted_item) - size_fn(item)
                if growth > 0:
                    budget.reserve_estimated_result_bytes(growth)
            result.data[index] = redacted_item
            redaction_count += item_count
    return redaction_count


def materialize_output_artifact(
    config: ScrapeConfig,
    all_results: list[TargetResult],
    job_id: str,
    final_status: JobStatus,
    all_errors: list[str],
    *,
    publish_results: bool,
    budget: RunBudget,
    now_fn: Callable[[], datetime] = utc_now,
    size_fn: Callable[[Any], int] = compact_json_size,
) -> dict[str, Any]:
    redaction_count = (
        _redact_result_records(config, all_results, budget=budget, size_fn=size_fn)
        if publish_results
        else 0
    )
    output_data = format_output(
        config,
        all_results,
        job_id,
        final_status,
        all_errors,
        publish_results=publish_results,
        budget=budget,
        now_fn=now_fn,
        size_fn=size_fn,
    )
    if (
        publish_results
        and config.output.group_by == GroupBy.merge
        and config.resolved_secret_values
    ):
        # Every record of a target carries the same source, so each distinct
        # source is redacted and measured once.
        redacted_sources: dict[str, tuple[str, int, int]] = {}
        for item in output_data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("_source"), str):
                continue
            source = item["_source"]
            if source not in redacted_sources:
                redacted_source, source_count = redact_deployment_secrets_with_count(
                    source,
                    config.resolved_secret_values,
                )
                growth = len(redacted_source.encode("utf-8")) - len(source.encode("utf-8"))
                redacted_sources[source] = (redacted_source, source_count, growth)
            redacted_source, source_count, growth = redacted_sources[source]
            if growth > 0:
                budget.reserve_estimated_result_bytes(growth)
            item["_source"] = redacted_source
            redaction_count += source_count
    return _redact_output_artifact(
        config,
        output_data,
        records_already_redacted=publish_results,
        record_redaction_count=redaction_count,
        budget=budget,
        size_fn=size_fn,
    )
