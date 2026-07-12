"""Local-only coordination checkpoints for destructive release qualification.

The production default is a complete no-op. A checkpoint can block only when
qualification mode, an exact checkpoint name, and a runner sentinel are all
present. There is intentionally no HTTP or Redis control surface.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from scrapeyard.common.settings import get_settings

logger = logging.getLogger(__name__)

QUALIFICATION_CRASH_POINTS = frozenset(
    {
        "after_enqueue_before_claim",
        "after_claim_run_creation",
        "during_target_execution",
        "after_result_artifact_write",
        "during_run_finalization",
        "during_webhook_intent_transaction",
        "after_terminal_state_before_delivery_ack",
    }
)
QUALIFICATION_SENTINEL_CONTENT = "scrapeyard-item15-local-qualification-v1\n"


def qualification_checkpoint(point: str) -> None:
    """Block at *point* until the external runner kills or releases this process."""

    settings = get_settings()
    if not settings.qualification_mode or settings.qualification_crash_point != point:
        return
    if point not in QUALIFICATION_CRASH_POINTS:
        raise RuntimeError(f"Unknown qualification crash point: {point!r}")

    marker_dir = Path(settings.qualification_marker_dir)
    sentinel = marker_dir / "enabled"
    try:
        sentinel_content = sentinel.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Qualification checkpoint refused: the local runner sentinel is missing"
        ) from exc
    if sentinel_content != QUALIFICATION_SENTINEL_CONTENT:
        raise RuntimeError("Qualification checkpoint refused: invalid runner sentinel")

    reached = marker_dir / f"reached-{point}"
    reached.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
    logger.critical(
        "LOCAL QUALIFICATION CHECKPOINT REACHED crash_point=%s "
        "recovery_action=await_external_process_termination",
        point,
    )
    release = marker_dir / f"release-{point}"
    while not release.exists():
        time.sleep(0.05)
