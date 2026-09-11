"""Whether anyone is there, and how long the queue really is."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capacity:
    """Whether anyone is actually there, and how long the queue really is.

    The current design's worst habit was promising a colleague with nothing
    behind it. This is the smallest honest correction: before speaking, ask how
    many are waiting and whether the desk is open, and say only what those two
    numbers support.

    `per_hour` is deliberately optional. A desk whose throughput nobody has
    measured produces no estimate rather than a plausible one — the agent then
    promises a reference and no time, which is true.
    """

    open: bool = True
    per_hour: float | None = None
    """Escalations this desk closes per hour, measured. Not a target."""

    def estimate_s(self, depth: int) -> int | None:
        if not self.per_hour or self.per_hour <= 0:
            return None
        return int((depth / self.per_hour) * 3600)


__all__ = ["Capacity"]
