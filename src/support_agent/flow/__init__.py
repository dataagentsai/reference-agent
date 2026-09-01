"""What happens under load.

L8 · P3 and P5. Fan-out limits, backpressure, and behaviour when a provider
throttles rather than fails.

### Reads go in parallel; writes go in order

A model may emit several tool calls in one turn, and running them concurrently is
the point of parallel tool use. But **only reads are parallelised here.**

*The tension.* Serialising everything is simpler and wastes the latency that
parallel tool calls exist to reclaim. Parallelising everything is faster and
means a partial failure across several writes leaves the world in a state nobody
designed: two of four refunds issued, in an order that depended on scheduling.

*The resolution.* Concurrency for `READ`, sequence for anything else. A read that
fails costs a retry; a write that fails halfway costs a reconciliation, and the
compensating path in `resilience` is far easier to reason about when the writes
happened one at a time in a known order.

### Throttling is not failure

A 429 means *slow down*, and the correct response is to slow down — not to fail
the conversation and not to retry immediately into the same wall. `Throttle`
records the provider's own `retry-after` and makes everyone else wait behind it,
so one rate-limited call slows the fleet instead of every caller discovering the
limit independently.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

from support_agent import telemetry as tel

DEFAULT_FAN_OUT = 4
"""Deliberately small. The limit exists to bound *our* pressure on a provider
that is already rate-limiting us, not to saturate it faster."""


class Limiter:
    """Bounded concurrency. A semaphore with a name, so a wait is visible."""

    def __init__(self, limit: int = DEFAULT_FAN_OUT) -> None:
        if limit < 1:
            raise ValueError("fan-out limit must be at least 1")
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self.peak = 0
        self._active = 0

    async def run[T](self, work: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            self._active += 1
            self.peak = max(self.peak, self._active)
            try:
                return await work()
            finally:
                self._active -= 1


async def gather_bounded[T](
    work: Sequence[Callable[[], Awaitable[T]]],
    *,
    limit: int = DEFAULT_FAN_OUT,
    limiter: Limiter | None = None,
) -> list[T]:
    """Run concurrently, bounded, **and return results in the order given**.

    Ordering is not a nicety. Tool results are matched to the calls that produced
    them by position in the transcript, so a scheduler that returns them as they
    finish would silently attach each answer to the wrong question.
    """
    bounded = limiter or Limiter(limit)
    if not work:
        return []
    with tel.span("agent.flow.fanout", **{"agent.flow.count": len(work)}) as span:
        results = await asyncio.gather(*(bounded.run(w) for w in work))
        span.set_attribute("agent.flow.peak", bounded.peak)
        return list(results)


class Throttle:
    """Shared backpressure. One rate-limited call slows everyone behind it.

    Without something shared, every caller discovers the limit independently and
    they all discover it at the same moment — which is the behaviour that turns a
    rate limit into an outage.
    """

    def __init__(self) -> None:
        self._until = 0.0
        self.waits = 0

    def note_retry_after(self, seconds: float, *, now: float | None = None) -> None:
        """Record the provider's own instruction. We do not invent a backoff when
        we have been told one."""
        moment = now if now is not None else time.monotonic()
        self._until = max(self._until, moment + max(0.0, seconds))

    def delay(self, *, now: float | None = None) -> float:
        moment = now if now is not None else time.monotonic()
        return max(0.0, self._until - moment)

    @property
    def throttled(self) -> bool:
        return self.delay() > 0

    async def wait(self) -> None:
        pause = self.delay()
        if pause <= 0:
            return
        self.waits += 1
        with tel.span("agent.flow.throttled", **{"agent.flow.delay_s": pause}):
            await asyncio.sleep(pause)


def retry_after_of(error: object) -> float | None:
    """Read `retry-after` from a provider error, if it said one.

    Returns `None` rather than a guess. A caller that invents a backoff when the
    provider offered a real one is choosing to be wrong on purpose.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_FAN_OUT",
    "Limiter",
    "Throttle",
    "gather_bounded",
    "retry_after_of",
]
