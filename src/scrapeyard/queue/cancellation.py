"""Typed queue cancellation outcomes and cooperative run-activity checks."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from scrapeyard.storage.protocols import JobStore
from scrapeyard.storage.types import RunOwnershipError

logger = logging.getLogger(__name__)

CancellationCheckpoint = Callable[[str], Awaitable[None]]


class QueueDeliveryState(str, Enum):
    """Bounded arq delivery states relevant to durable lifecycle decisions."""

    queued = "queued"
    deferred = "deferred"
    in_progress = "in_progress"
    complete = "complete"
    missing = "missing"


class QueueCancellationOutcome(str, Enum):
    """Run-scoped Redis/arq cancellation and quiescence outcome."""

    queued_cancelled = "queued_cancelled"
    deferred_cancelled = "deferred_cancelled"
    in_progress_cancelled = "in_progress_cancelled"
    complete = "complete"
    missing = "missing"
    timeout = "timeout"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class RunCancellationResult:
    """Typed observation returned after bounded arq cancellation."""

    run_id: str
    outcome: QueueCancellationOutcome
    initial_state: QueueDeliveryState | None = None
    final_state: QueueDeliveryState | None = None

    @property
    def quiescent(self) -> bool:
        return self.outcome not in {
            QueueCancellationOutcome.timeout,
            QueueCancellationOutcome.unavailable,
        }


async def cancellation_checkpoint(
    checkpoint: CancellationCheckpoint | None,
    name: str,
) -> None:
    """Invoke an optional run-activity guard at one explicit lifecycle boundary."""

    if checkpoint is not None:
        await checkpoint(name)


class RunActivityGuard:
    """Prove exact running ownership at bounded cooperative checkpoints."""

    def __init__(self, *, job_store: JobStore, job_id: str, run_id: str) -> None:
        self._job_store = job_store
        self.job_id = job_id
        self.run_id = run_id

    async def checkpoint(self, name: str) -> None:
        if await self._job_store.run_is_active(self.job_id, self.run_id):
            return
        logger.info(
            "Cooperative worker cancellation checkpoint rejected "
            "job_id=%s run_id=%s cancellation_checkpoint=%s "
            "ownership_outcome=lost recovery_action=stop_run_side_effects",
            self.job_id,
            self.run_id,
            name,
        )
        raise RunOwnershipError(
            f"cancellation checkpoint {name}",
            self.job_id,
            self.run_id,
        )
