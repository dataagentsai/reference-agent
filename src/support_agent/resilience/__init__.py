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
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from agent_harness import telemetry as tel
from agent_harness.contracts.failures import AgentFailure, Fault
from support_agent.contracts import (
    LLMClient,
    ModelBudgetExhausted,
    ModelRefused,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
)


class Retryable(AgentFailure):
    """Marker for a failure worth trying again. Everything else is not."""

    fault = Fault.UNREACHABLE


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


_UNIT: ContextVar[list[int] | None] = ContextVar("unit_retries", default=None)


@contextmanager
def unit_retries(limit: int) -> Iterator[None]:
    """One unit of work's retry allowance, shared by every call inside it.

    AHC-0024 and F-087: "a retry count exists per unit of work, not per call
    site". Each call kept its own few attempts, so a twelve-step turn could
    retry twenty-four times. Inside this scope every retry spends from one
    budget; when it is gone a failure is raised instead of retried. Outside any
    scope only the per-call bound applies.
    """
    token = _UNIT.set([limit])
    try:
        yield
    finally:
        _UNIT.reset(token)


def _may_retry() -> bool:
    """Spend one retry from the unit's allowance, if there is a unit and any left."""
    left = _UNIT.get()
    if left is None:
        return True
    if left[0] <= 0:
        return False
    left[0] -= 1
    return True


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

    async def wait(self, sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        pause = self.delay()
        if pause <= 0:
            return
        self.waits += 1
        with tel.span("agent.flow.throttled", **{"agent.flow.delay_s": pause}):
            await (sleep or asyncio.sleep)(pause)


class _Retry(Exception):  # noqa: N818 — control flow, carries the provider's failure
    def __init__(self, cause: ModelUnavailable) -> None:
        super().__init__(str(cause))
        self.cause = cause


class ResilientLLM:
    """L2 · L10. Any model client, behind a declared degradation path.

    Three controls, each answering a different condition — and the reason they
    are one decorator is that they must agree about which condition this is:

    - **A rate limit** (`ModelThrottled`) waits exactly the provider's
      `retry-after`, shared across every caller, and is **not** a breaker
      failure — being told to slow down is not evidence the provider is down
      (AHC-0021).
    - **A failure** (`ModelUnavailable`) is retried with capped, jittered backoff,
      a bounded number of times, each retry a span (AHC-0024), and counts
      towards the breaker.
    - **An open breaker** fails fast without calling the provider at all, until a
      single probe is allowed after the cooldown (AHC-0005).

    Malformed output is none of these and is never retried: the same prompt to
    the same model produced something unreadable, and a retry mostly buys a
    second bill. When the attempts run out, the last provider failure is raised
    typed, and the loop turns it into the customer's `Failed`.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        attempts: int = 3,
        backoff: Backoff | None = None,
        breaker: CircuitBreaker | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.inner = inner
        self.attempts = attempts
        self.backoff = backoff or Backoff()
        self.breaker = breaker or CircuitBreaker()
        self.throttle = throttle or Throttle()
        self._sleep = sleep or asyncio.sleep

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.breaker.closed:
            with tel.span("agent.breaker", **{"agent.breaker.state": self.breaker.state.value}):
                raise ModelUnavailable("the model provider is not answering; not calling it yet")
        # Counted per call, never on the client: one client serves every
        # conversation in the process, and a count it held was reset by whichever
        # call started next, renumbering another's retries (F-067).
        tries = [0]
        try:
            return await with_retry(
                partial(self._attempt, request, tries),
                attempts=self.attempts,
                backoff=self.backoff,
                retry_on=(_Retry,),
                sleep=self._sleep,
            )
        except _Retry as exhausted:
            raise exhausted.cause from None

    async def _attempt(self, request: ModelRequest, tries: list[int]) -> ModelResponse:
        tries[0] += 1
        await self.throttle.wait(self._sleep)
        try:
            response = await self.inner.complete(request)
        except (ModelRefused, ModelBudgetExhausted):
            # Asked and answered no, or a bound was reached: retrying gets the
            # same answer, and neither is evidence the provider is down.
            raise
        except ModelThrottled as exc:
            if exc.retry_after is not None:
                self.throttle.note_retry_after(exc.retry_after)
            if not _may_retry():
                raise
            self._visible_retry("throttled", tries[0])
            raise _Retry(exc) from exc
        except ModelUnavailable as exc:
            self.breaker.record_failure()
            if not _may_retry():
                raise
            self._visible_retry("unavailable", tries[0])
            raise _Retry(exc) from exc
        self.breaker.record_success()
        return response

    def _visible_retry(self, reason: str, tried: int) -> None:
        if tried < self.attempts:
            with tel.span(
                "agent.llm.retry",
                **{"agent.retry.attempt": tried, "agent.retry.reason": reason},
            ):
                pass


# --------------------------------------------------------------------------- #
# Compensation. AHC-0058 — declared, never inferred.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Compensation:
    """How one action is undone, and what it costs to do so."""

    action: str
    undo_action: str
    note: str


class NoCompensation(AgentFailure):
    """This action cannot be undone.

    Raised rather than returning `None`, because a caller that treats "no
    compensation" as "nothing to do" has silently decided an irreversible action
    was reversible.
    """

    fault = Fault.MISCONFIGURED


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
    "unit_retries",
    "ResilientLLM",
    "Throttle",
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
