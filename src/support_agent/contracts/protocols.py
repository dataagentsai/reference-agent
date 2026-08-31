"""The seams.

Every substitutable boundary in the system is declared here, in the bottom
layer, as a structural type. Upper layers depend on these shapes; adapters
satisfy them without importing anything. A composition root wires the two
together and is the only place that knows about both.

This file is why simulation is possible at all. Without a declared seam there is
nothing to intercept, and no way to run the system against a world that is not
the real one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from support_agent.contracts.ids import IdempotencyKey, Identity, RunId
from support_agent.contracts.model import ModelRequest, ModelResponse
from support_agent.contracts.tools import ToolRegistry, ToolResult


@runtime_checkable
class LLMClient(Protocol):
    """AHC-0022 — provider interaction is substitutable without changing the
    system. One choke point, every call passes through it."""

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class ToolClient(Protocol):
    """The MCP boundary — and the point at which a simulation intercepts.

    Two signatures carry design decisions rather than convenience:

    `list_tools` takes an identity because the advertised surface is
    authorization-scoped, so two customers may legitimately see different tools.

    `call` takes an idempotency key as a *required* argument, so it is
    impossible to invoke a tool without having minted one. The key is derived
    at the tool boundary from run, step and iteration — a retry keeps all three,
    a legitimate second execution changes the last. AHC-0074 is enforced by the
    type system rather than by remembering.
    """

    async def list_tools(self, identity: Identity) -> ToolRegistry: ...

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
    ) -> ToolResult: ...


@runtime_checkable
class Store(Protocol):
    """Durability at P6 — the only position whose enforcement survives process
    death, which is what makes it the right home for anything that must outlive
    a crash.

    Agent-owned state only. This never reaches the simulated business world:
    conversation, checkpoints, approvals and the idempotency ledger are the
    oracle, and an oracle that can be faked is not one.
    """

    async def checkpoint(self, run_id: RunId, state: bytes) -> None: ...

    async def resume(self, run_id: RunId) -> bytes | None: ...

    async def seen(self, key: IdempotencyKey) -> ToolResult | None:
        """The result of a previous execution under this key, if any.

        Present means the effect already happened. Returning the stored result
        rather than re-executing is the difference between one refund and two.
        """
        ...

    async def record(self, key: IdempotencyKey, result: ToolResult) -> None: ...


@runtime_checkable
class Clock(Protocol):
    """Time is injected, never read from the wall.

    A frozen clock is the cheapest way to make "return on day 31" a
    deterministic test rather than one that passes until the calendar moves.
    """

    def now_ms(self) -> int: ...
