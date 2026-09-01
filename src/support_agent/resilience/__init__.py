"""What happens when something breaks.

L10 · P3 and P5. Retries, a breaker, and compensation.

### Two different problems, often confused

**Idempotency** stops an effect happening *twice*. **Compensation** undoes one
that should not have happened *once*. The refund path needs both, and neither
substitutes for the other: a perfectly deduplicated refund that should never have
been issued is still a refund that has to be reversed.

AHC-0058 puts it as a declaration rather than an inference — *each action class
declares its compensating path*. So compensation here is a **registry**, not a
guess. An action with no registered compensation is one that cannot be undone,
and saying so out loud is more useful than a plausible default that quietly does
the wrong thing.

### What may be retried

Only operations that are safe to repeat. Reads always; writes **only because the
idempotency ledger sits underneath them at the tool boundary**, so a repeat is
recognised downstream rather than re-executed. Retry above a boundary that does
not deduplicate is how a timeout becomes a double charge — the mechanism is at
P5, and this module is only safe because that one exists.

### The breaker fails fast, and says so

An open breaker returns immediately with the declared degradation path (AAC-0009)
rather than queueing. A caller waiting behind a dependency that is known to be
down is a caller learning nothing slowly.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from support_agent import telemetry as tel


class Retryable(Exception):
    """Marker for a failure worth trying again. Everything else is not."""


@dataclass(frozen=True)
class Backoff:
    """Exponential, capped, with jitter.

    Jitter is not decoration. Without it every client that failed at the same
    moment retries at the same moment, and the recovering dependency is knocked
    over by the thundering herd it just created.
    """

    base_s: float = 0.2
    factor: float = 2.0
    max_s: float = 8.0
    jitter: float = 0.25

    def delay(self, attempt: int, *, rand: Callable[[], float] | None = None) -> float:
        raw = min(self.base_s * (self.factor**attempt), self.max_s)
        spread = raw * self.jitter
        draw = (rand or random.random)()
        return max(0.0, raw - spread + 2 * spread * draw)


async def with_retry[T](
    work: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    backoff: Backoff | None = None,
    retry_on: tuple[type[BaseException], ...] = (Retryable,),
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Try, back off, try again — and give up loudly.

    The final failure is re-raised rather than converted. A retry helper that
    swallows the last error leaves the caller unable to tell "it worked" from
    "we stopped asking".
    """
    policy = backoff or Backoff()
    pause = sleep or asyncio.sleep
    last: BaseException | None = None

    for attempt in range(attempts):
        try:
            return await work()
        except retry_on as exc:
            last = exc
            if attempt == attempts - 1:
                break
            await pause(policy.delay(attempt))

    assert last is not None
    raise last


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Stop asking a dependency that is not answering.

    Half-open lets exactly one probe through after the cooldown. Letting several
    through would re-open the breaker on the strength of a dependency that is
    only half recovered, and the probe is cheap precisely because it is one.
    """

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_s: float = 10.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._now = now or time.monotonic
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._now() - self._opened_at >= self.cooldown_s:
            self._state = BreakerState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._state = BreakerState.OPEN
            self._opened_at = self._now()

    @property
    def closed(self) -> bool:
        return self.state is not BreakerState.OPEN

    async def call[T](self, work: Callable[[], Awaitable[T]]) -> T:
        current = self.state
        if current is BreakerState.OPEN:
            with tel.span("agent.breaker", **{"agent.breaker.state": current.value}):
                raise Retryable("circuit is open; not calling a dependency known to be down")
        try:
            result = await work()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


# --------------------------------------------------------------------------- #
# Compensation. AHC-0058 — declared, never inferred.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Compensation:
    """How one action is undone, and what it costs to do so."""

    action: str
    undo_action: str
    note: str


class NoCompensation(Exception):
    """This action cannot be undone.

    Raised rather than returning `None`, because a caller that treats "no
    compensation" as "nothing to do" has silently decided an irreversible action
    was reversible.
    """


COMPENSATIONS: Mapping[str, Compensation] = {
    "issue_refund": Compensation(
        action="issue_refund",
        undo_action="reverse_refund",
        note="A refund is money already moved. Reversal is a second ledger entry, "
        "not an erasure, and the customer sees both.",
    ),
    "cancel_order": Compensation(
        action="cancel_order",
        undo_action="reinstate_order",
        note="Reinstatement depends on stock still being there, so this can fail "
        "in a way the original action could not.",
    ),
    "open_return_request": Compensation(
        action="open_return_request",
        undo_action="close_return_request",
        note="Reversible by design — the request is a record, not a movement.",
    ),
}


def compensation_for(action: str) -> Compensation:
    try:
        return COMPENSATIONS[action]
    except KeyError as exc:
        raise NoCompensation(
            f"{action!r} has no declared compensating path — "
            "it cannot be undone, and pretending otherwise is worse than saying so"
        ) from exc


def is_compensable(action: str) -> bool:
    return action in COMPENSATIONS


__all__ = [
    "COMPENSATIONS",
    "Backoff",
    "BreakerState",
    "CircuitBreaker",
    "Compensation",
    "NoCompensation",
    "Retryable",
    "compensation_for",
    "is_compensable",
    "with_retry",
]
