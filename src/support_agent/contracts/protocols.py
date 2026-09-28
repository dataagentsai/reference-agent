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
from support_agent.contracts.ids import (
    ConversationId,
    IdempotencyKey,
    Identity,
    RunId,
    StoredSession,
)
from support_agent.contracts.model import ModelRequest, ModelResponse
from support_agent.contracts.requests import CLAIM_TTL_S, Claim, Scope
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
        self,
        run_id: RunId,
        state: bytes,
        *,
        conversation_id: ConversationId,
        customer_id: str = "",
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

    async def forget(self, customer_id: str) -> tuple[RunId, ...]:
        """Remove everything held for this customer, and say which runs went.

        F-056. The store held their conversations and had no way to find them:
        what identifies a person is inside the serialized state, and serialized
        state cannot be searched. `customer_id` on the row is what makes this
        answerable at all, and it is why the method belongs on the seam rather
        than in a script somebody runs with a connection.

        **It returns the run ids** because they are the only handle anything
        else has on what this person's turns did. The request ledger is keyed
        by run, and once these rows are gone nothing can derive that list
        again — so a caller that erases the conversations first and looks for
        the ledger rows afterwards finds nothing, every time.
        """
        ...

    async def expire(self, before: int) -> int:
        """Remove every turn last written before `before` (epoch seconds), and
        say how many runs went — Q-RETENTION, AAC-0095.

        Erasure's time-based sibling: `forget` asks *whose*, this asks *how
        old*. The moment is the caller's, never the store's own reading of the
        wall, so a retention run is as testable as any other expiry here.
        """
        ...


@runtime_checkable
class Requests(Protocol):
    """The seam. Three calls, and the rule lives in the caller's choice between
    the last two."""

    async def claim(self, name: str, *, scope: Scope, ttl_s: int = CLAIM_TTL_S) -> Claim:
        """Take the name, or refuse. Raises `AlreadyAnswered` where a definite
        answer is stored, `StillRunning` where somebody unexpired holds it."""
        ...

    async def settle(self, name: str, outcome: dict[str, object] | None = None) -> None:
        """A definite answer. Stored, and the name is released — a later caller
        under this name is repeating it and gets this back."""
        ...

    async def abandon(self, name: str) -> None:
        """No definite answer. Nothing is stored and the name is free again, so
        a retry may go out **under the same name** for the far end to recognise.
        Storing a guess here is how one refund becomes two."""
        ...

    async def redact(self, runs: tuple[RunId, ...]) -> int:
        """Forget what these runs' calls answered, keeping that they happened.

        F-056, and the answer to the question it left open. Deleting the rows
        would make every one of those calls executable again — a replayed
        refund, months later, for somebody who asked to be forgotten. Keeping
        them intact keeps a record of what was done for that person.

        So neither: the name and the fact of a definite answer stay, and the
        answer itself goes. The guard is unchanged, because the guard was never
        the stored body — it is the name being taken. What a caller loses is
        the ability to be told what the first attempt said, which is the right
        thing to lose, and both replay paths already have to handle an answer
        that is not there.

        Returns how many rows were changed.
        """
        ...

    async def expire(self, before: int) -> int:
        """Delete names recorded before `before` (epoch seconds) that are
        answered or whose claim has lapsed — Q-RETENTION, AAC-0095.

        **Deleted, not tombstoned**, unlike `redact`, and the difference is
        time. A redacted row may be replayed tomorrow; a name more than a
        retention window old is not re-presented by anything: a delivery name
        is the channel's, which redelivers for minutes, and a tool name carries
        a run id whose checkpoint expires in the same pass, so nothing can
        resume the run that would repeat it. `erasure.retention` refuses a
        window short enough to break that. A live claim is never removed.
        """
        ...


@runtime_checkable
class ApprovalRecords(Protocol):
    """What the far end reads to check a call names a live grant (T-002)."""

    async def get(self, approval_id: str) -> Approval | None: ...


@runtime_checkable
class Approvals(ApprovalRecords, Protocol):
    """What the agent may do with approvals: ask, and read — P6 and P8.

    **No write** (T-028). It was a store with `put`, so the agent that asked for
    a refund could also record it granted, and granted small refunds itself. Now
    a request starts a workflow that assesses the action, waits for a person or
    an expiry, and carries the action out; a decision goes to that workflow
    through a separate desk the agent is never handed.
    """

    async def request(
        self,
        *,
        action: str,
        args: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
        conversation_id: str = "",
    ) -> Approval:
        """Start (or find) the approval for this call, and return it once it is
        assessed: carried out already, waiting for a person, or failed."""
        ...

    async def pending(self) -> tuple[Approval, ...]:
        """The queue a reviewer sees — P8."""
        ...


@runtime_checkable
class SessionStore(Protocol):
    """Where a customer's login waits for a channel to act on it (T-026).

    A refresh token is a credential that outlives the page it came from, so a
    durable store keeps it encrypted, and deleting it is how logout reaches the
    agent: no stored session, no turn.
    """

    async def put(self, session: StoredSession) -> None: ...

    async def get(self, subject: str) -> StoredSession | None: ...

    async def delete(self, subject: str) -> None: ...

    async def expire(self, before: int) -> int:
        """Delete logins last refreshed before `before` (epoch seconds) and say
        how many went — Q-RETENTION. A customer idle that long logs in again."""
        ...


@runtime_checkable
class Escalations(Protocol):
    """Where a conversation waits for a person — P6, the same argument as
    `Approvals` and one step larger: a lost approval loses one action, a lost
    escalation loses a customer nobody knows is waiting.

    **No write, and no close** (T-028). The agent raises one and reads it; a
    colleague closes it through a desk the agent is never handed, and the lapse
    is the workflow's timer rather than anybody's sweep.

    `open_for` rather than `get` on the read path the agent uses. The agent never
    holds an escalation id it did not just write, and a conversation is the handle
    it *does* hold — the same F-006 lesson the checkpoint store already learned.
    """

    async def raise_for(
        self,
        *,
        conversation_id: str,
        run_id: str,
        customer_id: str,
        reason: str,
        rule_id: str,
        rules_version: str,
        context: str = "",
        tier: int = 1,
        ttl_s: int = ...,
    ) -> Escalation: ...

    async def get(self, escalation_id: str) -> Escalation | None: ...

    async def open_for(self, conversation_id: str) -> Escalation | None:
        """The unresolved escalation on this conversation, if any."""
        ...

    async def pending(self) -> tuple[Escalation, ...]:
        """The queue a reviewer sees — P8."""
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
