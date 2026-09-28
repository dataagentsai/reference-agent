"""One name, one outcome — L10 · P6.

Two stores answered the same question with opposite rules until T-062. The
delivery claim settled on failure, so *tried counts as done*; the idempotency
ledger did not record one, so *failed means try again*. Same idea, one layer
apart, and which answer you got depended on where you were standing.

They are one thing now, and the rule is one sentence:

    A name is owed until there is a definite answer. A definite answer is
    stored, and returned to anyone who repeats the name.

**The word carrying it is `definite`.** There are three outcomes, not two:

    definite success    the store cancelled it        stored, released
    definite refusal    "shipped, cannot cancel"      stored, released
    indefinite          timeout, unreachable, crash   not stored, still owed

The third row is the whole design. A timeout means *it may or may not have
happened*. You must not store an outcome you do not know, and you must not keep
the claim — because the retry has to go out under the same name, and only the
far end can recognise it. This is the same distinction `llm` already makes for
model calls with `ModelRefused` against `ModelUnavailable`; it did not exist on
this side.

**Scope is a column, not a store.** A message name and a tool-call name are both
names. `delivery` guards a whole turn against a redelivered webhook;
`tool` guards one irreversible call against a retry inside one run. The rule
does not change between them, so neither does the code.

**The expiry is checked when a claim is taken**, which is why this needs no
sweeper and no workflow. A claim whose holder was killed outright never settles
— `finally` does not run for `kill -9` — so an abandoned claim is reclaimed by
the next caller finding it stale, rather than by anything going looking.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from support_agent.contracts.protocols import Requests
from support_agent.contracts.requests import (
    CLAIM_TTL_S,
    AlreadyAnswered,
    Claim,
    RequestRefused,
    Scope,
    StillRunning,
)


class _Held(StrEnum):
    IN_FLIGHT = "in_flight"
    ANSWERED = "answered"


@dataclass
class _Row:
    state: _Held
    outcome: dict[str, object] | None = None
    expires_at: float = 0.0
    recorded_at: int = 0
    """Epoch seconds, from `InMemoryRequests.clock` — what retention reads."""


@dataclass
class InMemoryRequests:
    """Process-local. Correct within one process, and wrong across a restart.

    Fine for tests and for a sealed simulation, where the process outlives the
    scenario by construction. **Not fine in production** — the failure this
    exists to prevent is the one that happens when a process dies mid-call, and
    this implementation dies with it. `PostgresRequests` replaces it at P6.
    """

    durable: bool = False
    now: Callable[[], float] = time.monotonic
    """The clock, injected like every other one here: a module that read the
    wall clock itself would be untestable about the one thing it is about."""
    clock: Callable[[], int] = field(default=lambda: int(time.time()))
    """Epoch seconds, for when a name was recorded (Q-RETENTION). Separate from
    `now`, which is monotonic and measures a claim's lease, not a date."""
    _rows: dict[str, _Row] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def claim(self, name: str, *, scope: Scope, ttl_s: int = CLAIM_TTL_S) -> Claim:
        async with self._lock:
            row = self._rows.get(name)
            if row is not None and row.state is _Held.ANSWERED:
                raise AlreadyAnswered(f"{name!r} was already answered", outcome=row.outcome)
            if row is not None and row.expires_at > self.now():
                raise StillRunning(f"{name!r} is already being handled")
            self._rows[name] = _Row(
                _Held.IN_FLIGHT, expires_at=self.now() + ttl_s, recorded_at=self.clock()
            )
            return Claim(name=name, scope=scope)

    async def settle(self, name: str, outcome: dict[str, object] | None = None) -> None:
        async with self._lock:
            # `setdefault`-shaped on purpose: the first definite answer for a
            # name is the answer. A later attempt under the same name did not
            # happen twice, so it must not be able to rewrite what did.
            row = self._rows.get(name)
            if row is None or row.state is _Held.ANSWERED:
                # Nothing held it, or it is already answered. The first definite
                # answer for a name is the answer: a later attempt under that
                # name did not happen twice, so it must not rewrite what did.
                return
            self._rows[name] = _Row(_Held.ANSWERED, outcome=outcome, recorded_at=row.recorded_at)

    async def abandon(self, name: str) -> None:
        async with self._lock:
            row = self._rows.get(name)
            if row is not None and row.state is _Held.IN_FLIGHT:
                del self._rows[name]

    async def redact(self, runs: tuple[str, ...]) -> int:
        """Keep the name, drop the answer — F-056."""
        wanted = set(runs)
        if not wanted:
            return 0
        async with self._lock:
            names = [n for n in self._rows if n.split(":")[0] in wanted]
            for name in names:
                row = self._rows[name]
                self._rows[name] = _Row(
                    row.state, outcome=None, expires_at=row.expires_at, recorded_at=row.recorded_at
                )
            return len(names)

    async def expire(self, before: int) -> int:
        """Names recorded before `before`, answered or lapsed — Q-RETENTION.
        Deleted rather than redacted: see `Requests.expire` for why age makes
        the difference that erasure does not."""
        async with self._lock:
            aged = [
                n
                for n, r in self._rows.items()
                if r.recorded_at < before
                and (r.state is _Held.ANSWERED or r.expires_at <= self.now())
            ]
            for name in aged:
                del self._rows[name]
            return len(aged)

    def __len__(self) -> int:
        return sum(1 for r in self._rows.values() if r.state is _Held.ANSWERED)


@asynccontextmanager
async def once(
    requests: Requests | None,
    name: str | None,
    *,
    scope: Scope,
    ttl_s: int = CLAIM_TTL_S,
) -> AsyncIterator[Claim | None]:
    """Hold a name for the length of the body, and settle it with what the body
    answered.

    A `None` name means the caller did not identify this request, and the body
    runs unguarded — the honest behaviour, because inventing a name here would
    produce a guard that can never fire and a green report to go with it.

    **Setting `claim.outcome` says the answer is definite**, and it is stored and
    returned to whoever repeats the name. Leaving it unset says nothing, and what
    that means is the scope's to decide — see `Scope`. The judgement lives there
    rather than here so that it is made once, in the open, instead of by each
    caller remembering.
    """
    if requests is None or name is None:
        yield None
        return

    claim = await requests.claim(name, scope=scope, ttl_s=ttl_s)
    try:
        yield claim
    finally:
        if claim.outcome is not None or scope.answered_by_arriving:
            await requests.settle(name, claim.outcome)
        else:
            await requests.abandon(name)


__all__ = [
    "RequestRefused",
    "CLAIM_TTL_S",
    "AlreadyAnswered",
    "Claim",
    "InMemoryRequests",
    "RequestRefused",
    "Requests",
    "Scope",
    "StillRunning",
    "once",
]
