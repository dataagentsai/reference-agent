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

from support_agent.contracts.human import Escalation
from support_agent.contracts.ids import ConversationId, IdempotencyKey, Identity, RunId
from support_agent.contracts.model import ModelRequest, ModelResponse
from support_agent.contracts.tools import Approval, ToolRegistry, ToolResult


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
class CheckpointStore(Protocol):
    """Durability at P6 for conversation and loop state — L5.

    Agent-owned. This never reaches the simulated business world: conversation,
    checkpoints and approvals are the oracle, and an oracle that can be faked is
    not one.
    """

    async def checkpoint(
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None: ...

    async def resume(self, run_id: RunId) -> bytes | None: ...

    async def latest(self, conversation_id: ConversationId) -> bytes | None: ...

    """The most recent state of a conversation — F-006.

    Checkpoints are filed under **run** id and a fresh run id is minted every
    turn, so `resume` can only be called by something that already knows the
    run — which a customer never does. A caller holds a *conversation* id, and
    until this existed there was no way to turn one into state.

    Nothing noticed for weeks because every test passes the conversation object
    through in memory. It took writing an actual HTTP handler for the gap to
    become unavoidable: that handler cannot be written without this method.
    """


@runtime_checkable
class IdempotencyLedger(Protocol):
    """The dedupe record at P6 — L10.

    Split from `CheckpointStore` deliberately. `state` and `idempotency` are
    sibling modules that may not import each other, and a single protocol
    spanning both would have forced one of them to depend on the other's
    concerns. The architecture contract surfaced the design error; this is the
    fix, not a workaround.
    """

    async def seen(self, key: IdempotencyKey) -> ToolResult | None:
        """The result of a previous execution under this key, if any.

        Present means the effect already happened. Returning the stored result
        rather than re-executing is the difference between one refund and two.
        """
        ...

    async def record(self, key: IdempotencyKey, result: ToolResult) -> None: ...


@runtime_checkable
class ApprovalStore(Protocol):
    """Where a pending decision waits — P6, because a human may take an hour and
    a store that dies with the process is absent exactly when it was needed."""

    async def put(self, approval: Approval) -> None: ...

    async def get(self, approval_id: str) -> Approval | None: ...

    async def pending(self) -> tuple[Approval, ...]:
        """The queue a reviewer sees — P8."""
        ...


@runtime_checkable
class EscalationStore(Protocol):
    """Where a conversation waits for a person — P6, same argument as
    `ApprovalStore` and one step larger: a lost approval loses one action, a lost
    escalation loses a customer nobody knows is waiting.

    `open_for` rather than `get` on the read path the agent uses. The agent never
    holds an escalation id it did not just write, and a conversation is the handle
    it *does* hold — the same F-006 lesson the checkpoint store already learned.
    """

    async def put(self, escalation: Escalation) -> None: ...

    async def get(self, escalation_id: str) -> Escalation | None: ...

    async def open_for(self, conversation_id: str) -> Escalation | None:
        """The unresolved escalation on this conversation, if any."""
        ...

    async def pending(self) -> tuple[Escalation, ...]:
        """The queue a reviewer will see — P8. Nothing reads this yet; the
        reviewer surface is step 4."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time is injected, never read from the wall.

    A frozen clock is the cheapest way to make "return on day 31" a
    deterministic test rather than one that passes until the calendar moves.

    **Epoch seconds, and callable.** It said `now_ms()` until nothing had ever
    implemented it — and meanwhile every module that actually needed a moment
    grew its own `now: int | None = None` parameter in seconds. A declared seam
    that disagrees with the convention around it is worse than no seam: it reads
    as a decision when it is really a stale draft, and the simulator could not
    drive expiry through it. This is the shape the codebase already speaks, so
    `agenttwin.Clock` satisfies it without knowing this protocol exists.
    """

    def __call__(self) -> int: ...
