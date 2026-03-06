"""Core domain models for jobs, errors, and error filtering."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Possible states of a scrape job."""

    queued = "queued"
    running = "running"
    complete = "complete"
    partial = "partial"
    failed = "failed"


class ErrorType(str, Enum):
    """Classification of scrape errors."""

    content_empty = "content_empty"
    http_error = "http_error"
    network_error = "network_error"
    browser_error = "browser_error"
    timeout = "timeout"


class ActionTaken(str, Enum):
    """Action taken in response to an error."""

    retry = "retry"
    warn = "warn"
    fail = "fail"
    skip = "skip"
    circuit_break = "circuit_break"


class Job(BaseModel):
    """Represents a scrape job (on-demand or scheduled)."""

    job_id: str = Field(..., description="Unique job identifier")
    project: str = Field(..., description="Project namespace")
    name: str = Field(..., description="Job name within the project")
    status: JobStatus = Field(default=JobStatus.queued, description="Current job status")
    config_yaml: str = Field(..., description="Raw YAML config for this job")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    schedule_cron: Optional[str] = Field(default=None, description="Cron expression if scheduled")
    last_run_at: Optional[datetime] = None
    run_count: int = Field(default=0, description="Total number of runs")


class ErrorRecord(BaseModel):
    """Structured error record per spec section 5.2."""

    job_id: str
    project: str
    target_url: str
    attempt: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    error_type: ErrorType
    http_status: Optional[int] = None
    fetcher_used: str
    selectors_matched: Optional[dict[str, int]] = None
    action_taken: ActionTaken
    resolved: bool = False


class ErrorFilters(BaseModel):
    """Filters for querying error records."""

    project: Optional[str] = None
    job_id: Optional[str] = None
    since: Optional[datetime] = None
    error_type: Optional[ErrorType] = None
