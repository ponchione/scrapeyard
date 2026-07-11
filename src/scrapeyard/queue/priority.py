"""Deterministic priority routing and bounded weighted queue admission."""

from __future__ import annotations

from dataclasses import dataclass

PRIORITIES = ("high", "normal", "low")
FAIR_ADMISSION_CYCLE = (
    "high",
    "high",
    "high",
    "high",
    "normal",
    "normal",
    "low",
)


def priority_queue_names(base_queue_name: str) -> dict[str, str]:
    """Return the fixed intake queue name for each configured priority."""

    return {
        priority: f"{base_queue_name}:priority:{priority}"
        for priority in PRIORITIES
    }


@dataclass(slots=True)
class WeightedPriorityPolicy:
    """Work-conserving 4:2:1 weighted admission with a bounded fair turn."""

    cursor: int = 0

    @property
    def preferred(self) -> str:
        return FAIR_ADMISSION_CYCLE[self.cursor]

    def selection_order(self) -> tuple[str, ...]:
        """Prefer the current fair turn, then the highest available priority."""

        preferred = self.preferred
        return (preferred, *(priority for priority in PRIORITIES if priority != preferred))

    def admitted(self) -> None:
        """Advance exactly once after one delivery is admitted."""

        self.cursor = (self.cursor + 1) % len(FAIR_ADMISSION_CYCLE)
