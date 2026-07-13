"""Version-one HTTP response contracts exposed through OpenAPI."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class APICompatibility(str, Enum):
    v1 = "v1"
    legacy_v0 = "legacy-v0"


class ErrorDetail(APIResponseModel):
    location: list[str | int] = Field(default_factory=list)
    message: str
    type: str


class ErrorEnvelope(APIResponseModel):
    error: str
    code: str
    status_code: int
    details: list[ErrorDetail] = Field(default_factory=list)


class QueuedSubmissionResponse(APIResponseModel):
    job_id: str
    run_id: str
    status: str
    poll_url: str


class ResultTargetSummary(APIResponseModel):
    url: str
    status: str
    count: int
    observed_count: int
    debug: dict[str, Any] | None = None
    error_type: str | None = None
    error_detail: str | None = None
    pages_scraped: int
    errors: list[str] = Field(default_factory=list)


class ResultResponse(APIResponseModel):
    job_id: str
    run_id: str
    status: str
    completed_at: datetime | None = None
    errors: list[str] = Field(default_factory=list)
    targets: list[ResultTargetSummary] = Field(default_factory=list)
    budget_error: dict[str, Any] | None = None
    results: list[Any] | dict[str, Any]


class LegacyResultResponse(APIResponseModel):
    job_id: str
    run_id: str
    status: str
    results: Any


class JobRunResponse(APIResponseModel):
    run_id: str
    status: str
    trigger: str
    config_hash: str
    started_at: datetime
    heartbeat_at: datetime
    completed_at: datetime | None
    record_count: int | None
    error_count: int


class JobSummaryResponse(APIResponseModel):
    job_id: str
    project: str
    name: str
    status: str
    created_at: datetime
    updated_at: datetime | None
    schedule_cron: str | None
    schedule_timezone: str
    schedule_enabled: bool
    run_count: int
    last_run_at: datetime | None


class JobDetailResponse(JobSummaryResponse):
    config_yaml: str
    next_run_at: datetime | None
    runs: list[JobRunResponse]


class JobCreatedResponse(APIResponseModel):
    job_id: str
    project: str
    name: str
    schedule: str
    schedule_timezone: str


class ScheduleStateResponse(APIResponseModel):
    job_id: str
    project: str
    name: str
    status: str
    schedule_cron: str
    schedule_timezone: str
    schedule_enabled: bool
    config_hash: str
    updated_at: datetime | None


class ManualTriggerResponse(APIResponseModel):
    job_id: str
    run_id: str
    status: Literal["queued"]
    trigger: Literal["manual"]
    config_hash: str
    poll_url: str


class BudgetErrorResponse(APIResponseModel):
    limit_name: str
    configured_limit: int | float
    observed_amount: int | float


class ErrorRecordResponse(APIResponseModel):
    job_id: str
    run_id: str
    project: str
    target_url: str
    attempt: int
    timestamp: datetime
    error_type: str
    http_status: int | None
    fetcher_used: str
    error_message: str | None
    selectors_matched: dict[str, int] | None
    budget: BudgetErrorResponse | None
    action_taken: str
    resolved: bool = Field(
        description="Whether a subsequent validation retry corrected this error"
    )


class DependencyProbeResponse(APIResponseModel):
    ok: bool
    detail: str | None = None


class QueueDepthResponse(APIResponseModel):
    high: int | None
    normal: int | None
    low: int | None


class WorkerHealthResponse(APIResponseModel):
    max_concurrent: int
    active_tasks: int
    max_browsers: int
    active_browsers: int
    queue_depths: QueueDepthResponse


class HealthResponse(APIResponseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    uptime_seconds: float
    workers: WorkerHealthResponse
    dependencies: dict[str, DependencyProbeResponse]
    background_tasks: dict[str, DependencyProbeResponse]
    projects: dict[str, Any]


class LivenessResponse(APIResponseModel):
    status: Literal["ok"]


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope}
    for status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 500, 503, 504)
}

PAGINATION_HEADERS: dict[str, dict[str, Any]] = {
    "X-Scrapeyard-Limit": {"schema": {"type": "integer"}},
    "X-Scrapeyard-Offset": {"schema": {"type": "integer"}},
    "X-Scrapeyard-Item-Count": {"schema": {"type": "integer"}},
    "X-Scrapeyard-Has-More": {"schema": {"type": "boolean"}},
    "X-Scrapeyard-Next-Offset": {"schema": {"type": "integer"}},
}
