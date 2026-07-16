"""Service-level settings read from environment variables via Pydantic BaseSettings."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from scrapeyard.engine.proxy import normalize_service_proxy_url


_SECRET_REFERENCE_NAME_RE = re.compile(r"^SCRAPEYARD_SECRET_[A-Z0-9_]+$")


class ServiceSettings(BaseSettings):
    """Central service configuration populated from environment variables.

    All variables are prefixed with ``SCRAPEYARD_`` and grouped by subsystem.
    """

    workers_max_concurrent: int = Field(default=4, ge=1)
    workers_max_browsers: int = Field(default=2, ge=1)
    workers_memory_limit_mb: int = Field(default=4096, ge=0)
    sync_timeout_seconds: int = Field(default=15, ge=0)
    sync_poll_delay_seconds: float = Field(default=0.5, gt=0)
    basic_fetch_timeout_seconds: float = Field(default=30.0, gt=0)
    workers_shutdown_grace_seconds: int = Field(default=30, ge=0)
    workers_cancellation_grace_seconds: float = Field(default=10.0, gt=0)
    workers_queued_claim_timeout_seconds: int = Field(default=300, gt=0)
    workers_queue_payload_ttl_seconds: int = Field(default=604800, gt=0)
    workers_queued_reconciliation_interval_seconds: float = Field(default=60.0, gt=0)
    workers_queued_reconciliation_batch_size: int = Field(default=100, ge=1)
    workers_running_heartbeat_timeout_seconds: int = Field(default=600, gt=0)
    workers_running_reconciliation_interval_seconds: float = Field(default=60.0, gt=0)
    workers_running_reconciliation_batch_size: int = Field(default=100, ge=1)
    workers_heartbeat_interval_seconds: int = Field(default=30, gt=0)
    workers_redis_connect_timeout_seconds: float = Field(default=10.0, gt=0)

    run_max_duration_seconds: float = Field(default=900.0, gt=0)
    run_max_fetched_bytes: int = Field(default=104857600, ge=1)
    run_max_extracted_records: int = Field(default=100000, ge=1)
    run_max_serialized_result_bytes: int = Field(default=52428800, ge=4096)
    run_max_browser_debug_bytes: int = Field(default=26214400, ge=1)
    run_thread_max_workers: int = Field(default=4, ge=1)
    api_result_thread_max_workers: int = Field(default=2, ge=1)
    transform_regex_timeout_seconds: float = Field(default=0.1, gt=0, le=5)
    transform_regex_max_pattern_bytes: int = Field(default=2048, ge=1, le=16384)
    transform_max_pipeline_steps: int = Field(default=32, ge=1, le=256)
    transform_max_value_bytes: int = Field(default=1048576, ge=1024, le=52428800)

    redis_dsn: str = "redis://redis:6379/0"
    queue_name: str = "scrapeyard"

    admin_read_default_limit: int = Field(default=100, ge=1)
    admin_read_max_limit: int = Field(default=500, ge=1)

    idempotency_key_max_bytes: int = Field(default=128, ge=1, le=1024)
    idempotency_retention_hours: int = Field(default=24, ge=1)
    idempotency_cleanup_batch_size: int = Field(default=1000, ge=1)

    rate_limit_requests: int = Field(default=600, ge=0)
    rate_limit_window_seconds: int = Field(default=60, ge=0)
    rate_limit_max_keys: int = Field(default=10000, ge=1)

    scheduler_jitter_max_seconds: int = Field(default=120, ge=0)
    scheduler_misfire_grace_seconds: int = Field(default=60, ge=1)

    webhook_max_delivery_attempts: int = Field(default=5, ge=1)
    webhook_max_delivery_age_seconds: int = Field(default=86400, ge=1)
    webhook_dispatch_concurrency: int = Field(default=4, ge=1)
    webhook_dispatch_batch_size: int = Field(default=100, ge=1)
    webhook_client_cache_max_size: int = Field(default=64, ge=1)
    webhook_client_cache_idle_ttl_seconds: float = Field(default=300.0, gt=0)
    webhook_delivered_retention_days: int = Field(default=7, ge=1)
    webhook_failed_retention_days: int = Field(default=30, ge=1)

    history_adhoc_job_retention_days: int = Field(default=30, ge=1)
    history_scheduled_run_retention_days: int = Field(default=30, ge=1)
    history_scheduled_run_retention_count: int = Field(default=100, ge=1)
    history_error_retention_days: int = Field(default=30, ge=1)
    history_webhook_tombstone_retention_days: int = Field(default=30, ge=1)
    history_adhoc_job_cleanup_batch_size: int = Field(default=100, ge=1)
    history_scheduled_run_cleanup_batch_size: int = Field(default=500, ge=1)
    history_error_cleanup_batch_size: int = Field(default=1000, ge=1)

    storage_retention_days: int = Field(default=30, ge=1)
    db_dir: str = "/data/db"
    storage_results_dir: str = "/data/results"
    storage_max_results_per_job: int = Field(default=100, ge=1)
    storage_cleanup_batch_size: int = Field(default=500, ge=1)
    storage_orphan_grace_seconds: int = Field(default=86400, ge=1)
    storage_reconciliation_dry_run: bool = True
    storage_reconciliation_promotion_ack: str = ""
    storage_cleanup_interval_seconds: float = Field(default=21600.0, gt=0)
    storage_cleanup_cycle_max_items_per_phase: int = Field(default=10000, ge=1)
    storage_cleanup_cycle_max_seconds: float = Field(default=60.0, gt=0)
    storage_cleanup_catchup_delay_seconds: float = Field(default=5.0, gt=0)
    storage_reconciliation_max_entries_per_run: int = Field(default=10000, ge=1)
    adaptive_dir: str = "/data/adaptive"
    log_dir: str = "/data/logs"
    browser_debug_enabled: bool = False

    circuit_breaker_max_failures: int = Field(default=3, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(default=300, ge=0)
    circuit_breaker_max_domains: int = Field(default=10000, ge=1)
    circuit_breaker_inactive_ttl_seconds: float = Field(default=3600.0, gt=0)
    proxy_url: str = ""
    untrusted_submissions: bool = False
    egress_policy_probe_host: str = ""
    egress_policy_probe_port: int = Field(default=0, ge=0, le=65535)
    egress_policy_probe_liveness_port: int = Field(default=0, ge=0, le=65535)
    egress_policy_probe_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    log_level: str = "INFO"
    domain_rate_limit_shared: bool = True
    domain_rate_limit_max_domains: int = Field(default=10000, ge=1)

    api_keys: str = ""
    api_credentials: str = ""
    local_development_unauthenticated: bool = False
    health_probe_api_key: str = Field(default="", repr=False)
    secret_reference_allowlist: str = ""
    encryption_keys: str = Field(default="", repr=False)
    encryption_active_key_id: str = ""
    max_request_bytes: int = Field(default=262144, ge=0)
    health_disk_free_min_mb: int = Field(default=100, ge=0)
    health_include_projects: bool = False
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    metrics_refresh_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    # Destructive release qualification only. A crash point also requires the
    # in-container runner sentinel; there is no remote trigger.
    qualification_mode: bool = False
    qualification_crash_point: str = ""
    qualification_marker_dir: str = "/run/scrapeyard-qualification"

    model_config = {
        "env_prefix": "SCRAPEYARD_",
        # Service delays and limits are resource-safety boundaries. Pydantic's
        # numeric comparisons otherwise accept positive infinity for fields
        # constrained only with ``gt=0``.
        "allow_inf_nan": False,
    }

    @field_validator("proxy_url")
    @classmethod
    def _normalize_proxy_url(cls, value: str) -> str:
        if not value.strip():
            return ""
        return normalize_service_proxy_url(value)

    @model_validator(mode="after")
    def _validate_read_limits(self) -> ServiceSettings:
        if self.admin_read_default_limit > self.admin_read_max_limit:
            raise ValueError("admin_read_default_limit must be <= admin_read_max_limit")
        if (
            self.workers_heartbeat_interval_seconds * 3
            > self.workers_running_heartbeat_timeout_seconds
        ):
            raise ValueError(
                "workers_heartbeat_interval_seconds must be no more than one third "
                "of workers_running_heartbeat_timeout_seconds"
            )
        if self.workers_queue_payload_ttl_seconds <= (
            self.workers_queued_claim_timeout_seconds
            + self.workers_queued_reconciliation_interval_seconds
        ):
            raise ValueError(
                "workers_queue_payload_ttl_seconds must exceed the queued claim "
                "timeout plus one reconciliation interval"
            )
        if self.webhook_dispatch_batch_size < self.webhook_dispatch_concurrency:
            raise ValueError("webhook_dispatch_batch_size must be >= webhook_dispatch_concurrency")
        promotion_ack = self.storage_reconciliation_promotion_ack.strip()
        if not self.storage_reconciliation_dry_run:
            if not promotion_ack:
                raise ValueError(
                    "storage_reconciliation_promotion_ack is required when destructive "
                    "reconciliation is enabled"
                )
            try:
                promoted_at = datetime.fromisoformat(
                    promotion_ack.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    "storage_reconciliation_promotion_ack must be an RFC 3339 timestamp"
                ) from exc
            if promoted_at.tzinfo is None:
                raise ValueError(
                    "storage_reconciliation_promotion_ack must include a timezone"
                )
            self.storage_reconciliation_promotion_ack = promotion_ack
        if self.qualification_crash_point and not self.qualification_mode:
            raise ValueError("qualification_crash_point requires qualification_mode=true")
        if self.qualification_crash_point:
            from scrapeyard.common.qualification import QUALIFICATION_CRASH_POINTS

            if self.qualification_crash_point not in QUALIFICATION_CRASH_POINTS:
                raise ValueError("qualification_crash_point must name a supported local checkpoint")
        probe_host = self.egress_policy_probe_host.strip()
        probe_configured = (
            bool(probe_host)
            or self.egress_policy_probe_port != 0
            or self.egress_policy_probe_liveness_port != 0
        )
        if probe_configured and (
            not probe_host
            or self.egress_policy_probe_port == 0
            or self.egress_policy_probe_liveness_port == 0
        ):
            raise ValueError(
                "egress_policy_probe_host, egress_policy_probe_port, and "
                "egress_policy_probe_liveness_port must be configured together"
            )
        if (
            probe_configured
            and self.egress_policy_probe_port == self.egress_policy_probe_liveness_port
        ):
            raise ValueError("egress policy challenge and liveness ports must be different")
        if probe_host:
            try:
                probe_address = ipaddress.ip_address(probe_host)
            except ValueError as exc:
                raise ValueError("egress_policy_probe_host must be an IP address") from exc
            if (
                probe_address.is_global
                or probe_address.is_unspecified
                or probe_address.is_multicast
            ):
                raise ValueError(
                    "egress_policy_probe_host must be a controlled non-public unicast address"
                )
            self.egress_policy_probe_host = probe_host
        if self.untrusted_submissions:
            if not self.proxy_url or self.proxy_url == "direct":
                raise ValueError(
                    "untrusted_submissions requires an egress-filtering operator proxy_url"
                )
            if not probe_configured:
                raise ValueError(
                    "untrusted_submissions requires a connected-IP egress policy probe"
                )
        self.parsed_secret_reference_allowlist()
        return self

    def parsed_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    def parsed_secret_reference_allowlist(self) -> dict[str, frozenset[str]]:
        """Return the project-to-secret-name policy for submitted YAML references."""

        if not self.secret_reference_allowlist.strip():
            return {}
        try:
            parsed = json.loads(self.secret_reference_allowlist)
        except json.JSONDecodeError as exc:
            raise ValueError("SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST must be a JSON object")

        policy: dict[str, frozenset[str]] = {}
        for project, names in parsed.items():
            if not isinstance(project, str):
                raise ValueError(
                    "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST project names must be strings"
                )
            if project != "*":
                from scrapeyard.common.paths import safe_path_part

                safe_path_part(project, label="secret-reference project")
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError(
                    "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST values must be lists of names"
                )
            invalid_names = [
                name for name in names if _SECRET_REFERENCE_NAME_RE.fullmatch(name) is None
            ]
            if invalid_names:
                raise ValueError(
                    "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST contains an invalid secret name"
                )
            if len(names) != len(set(names)):
                raise ValueError(
                    "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST contains duplicate secret names"
                )
            policy[project] = frozenset(names)
        return policy


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    """Return a cached singleton :class:`ServiceSettings` instance."""
    return ServiceSettings()
