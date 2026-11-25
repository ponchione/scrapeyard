"""Shared execution context dataclasses for scrape worker runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.settings import ServiceSettings
from scrapeyard.config.schema import PageCacheMode, ScrapeConfig
from scrapeyard.engine.domain_guard import RunDomainPolicy
from scrapeyard.engine.page_cache import PageCache
from scrapeyard.engine.proxy import new_proxy_session_token
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, ResultValidator
from scrapeyard.models.job import ErrorRecord, Job, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import RunActivityGuard
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.storage.types import SaveResultMeta


@dataclass
class OwnerFinalization:
    """The completed_at this worker gave finalize_owned_run, once it has."""

    completed_at: datetime | None = None


@dataclass(frozen=True)
class JobExecutionContext:
    config: ScrapeConfig
    config_hash: str
    job: Job
    settings: ServiceSettings
    started_at: datetime
    adaptive_dir: str
    run_artifacts_dir: str | None
    budget: RunBudget
    run_id: str
    activity: RunActivityGuard
    domain_policy: RunDomainPolicy | None = None
    page_cache: PageCache | None = None
    # The job asked for the page cache but the service has no cache directory.
    page_cache_unavailable: bool = False
    finalization: OwnerFinalization = field(default_factory=OwnerFinalization)

    @property
    def replaying(self) -> bool:
        return self.config.execution.page_cache is PageCacheMode.replay


@dataclass(frozen=True)
class PersistedJobResult:
    final_status: JobStatus
    flat_data: list[dict[str, Any]]
    all_errors: list[str]
    save_meta: SaveResultMeta


@dataclass(frozen=True)
class TargetProcessingContext:
    config: ScrapeConfig
    job_id: str
    run_id: str | None
    settings: ServiceSettings
    adaptive_dir: str
    run_artifacts_dir: str | None
    circuit_breaker: CircuitBreaker
    rate_limiter: DomainRateLimiter
    validator: ResultValidator
    browser_limiter: BrowserExecutionLimiter | None
    budget: RunBudget
    activity: RunActivityGuard
    # Replay serves recorded pages: no browser slots, pacing or circuit updates.
    replay: bool = False
    # One sticky proxy session per run: every target and page shares the token.
    proxy_session: str = field(default_factory=new_proxy_session_token, repr=False)

    def recorder(self, pending_errors: list[ErrorRecord]) -> TargetErrorRecorder:
        return TargetErrorRecorder(
            job_id=self.job_id,
            run_id=self.run_id,
            project=self.config.project,
            pending_errors=pending_errors,
            circuit_breaker=self.circuit_breaker,
        )
