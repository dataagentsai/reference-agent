"""What happens when the same thing is asked twice.

L10 · P6. The ledger that turns AHC-0074 from a requirement into a mechanism.

The trap this exists for is specific and is not "the model called it twice". It
is the timeout: **the call succeeded and the response was lost**, so the harness
believes it failed and retries, while the downstream system has already applied
the effect. Without a key recognised at the destination, nothing can tell the two
apart — and the difference is one refund or two.

P6 is the position because it is the only one whose enforcement survives process
death. A ledger in memory is a ledger that forgets exactly when it matters, which
is why `InMemoryLedger` below says so out loud.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from support_agent.contracts import IdempotencyKey, IdempotencyLedger, SideEffectClass, ToolResult


class InMemoryLedger:
    """Process-local. Correct within one run, and wrong across a restart.

    Fine for tests and for a sealed simulation, where the process outlives the
    scenario by construction. **Not fine in production** — the failure it exists
    to prevent is the one that happens when a process dies mid-call, and this
    implementation dies with it. The Postgres ledger replaces it at P6.
    """

    durable = False

    def __init__(self) -> None:
        self._entries: dict[str, ToolResult] = {}
        self._lock = asyncio.Lock()

    async def seen(self, key: IdempotencyKey) -> ToolResult | None:
        async with self._lock:
            return self._entries.get(key.value)

    async def record(self, key: IdempotencyKey, result: ToolResult) -> None:
        async with self._lock:
            self._entries.setdefault(key.value, result)
            # setdefault, not assignment: the first recorded outcome for a key
            # is the outcome. A later attempt under the same key did not happen
            # twice, so it must not be able to rewrite what did.

    def __len__(self) -> int:
        return len(self._entries)


async def once(
    ledger: IdempotencyLedger,
    key: IdempotencyKey,
    side_effect: SideEffectClass,
    action: Callable[[], Awaitable[ToolResult]],
) -> tuple[ToolResult, bool]:
    """Run `action` at most once per key. Returns the result and whether it was
    replayed from the ledger rather than executed.

    Reads bypass the ledger entirely: recording them costs storage and buys
    nothing, since repeating a read is free and invisible. Anything else is
    checked first, executed, then recorded — in that order, because recording
    before executing would suppress a retry after a genuine failure.
    """
    if side_effect is SideEffectClass.READ:
        return await action(), False

    previous = await ledger.seen(key)
    if previous is not None:
        return previous, True

    result = await action()
    if not result.is_error:
        await ledger.record(key, result)
        # Errors are not recorded. A tool that failed did not apply an effect,
        # so a retry under the same key must be allowed to reach it. Recording
        # failures would turn one transient 503 into a permanent refusal.
    return result, False


__all__ = ["InMemoryLedger", "once"]
