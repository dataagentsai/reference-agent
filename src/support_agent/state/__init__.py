"""What is remembered.

L5 · P6. Conversation, checkpoints, and the durability boundary between what
survives a crash and what does not.

**This is agent-owned state, and AgentTwin never projects it.** The business world
is simulated; conversation, checkpoints and approvals are the *oracle*. An oracle
that can be faked is not one — which is why this lives in its own schema with its
own credentials, and why `state` and the tool server never share a connection.

The durability boundary is stated per implementation rather than assumed.
`InMemoryCheckpointStore` says it does not survive a restart; `FileCheckpointStore`
says it does. P6 exists precisely because enforcement that dies with the process
is enforcement that is absent at the moment it was needed — an agent interrupted
mid-approval is the case, and it is not hypothetical.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from support_agent.contracts import (
    ConversationId,
    Direct,
    Escalated,
    Message,
    NeedsApproval,
    Route,
    RunId,
    TurnResult,
)

RECENT_TURNS = 12
"""How many turn outcomes are kept.

Bounded on purpose. A checkpoint is written every turn and read every turn, and
the docstring below is not decoration — an unbounded history is how a
conversation that runs all day becomes a row nobody can load. Twelve is more
than any Tier 2 rule looks back over, and the rules are what this exists for.
"""


class TurnNote(BaseModel):
    """One turn, reduced to what a rule can ask about.

    Not the reply, not the goal, not the tool calls — those are the trace's job.
    This is the smallest thing that lets *"they have asked three times and none
    of it worked"* be a computation rather than an impression.
    """

    model_config = ConfigDict(frozen=True)

    route: str
    """direct · agentic · refuse · escalate."""
    result: str
    """completed · refused · escalated · failed · needs_approval."""
    intent: str | None = None
    termination: str | None = None

    @classmethod
    def of(cls, decision: Route, result: TurnResult) -> TurnNote:
        """Reduce a turn to what a rule can ask about.

        The intent is only known on a `Direct` route; `Agentic` carries candidates
        rather than a decision, and recording a guess as a fact is how a rule ends up
        counting something nobody classified.
        """
        return cls(
            route=decision.kind,
            result=result.kind,
            intent=decision.intent.value if isinstance(decision, Direct) else None,
            termination=getattr(result, "termination", None),
        )


class Conversation(BaseModel):
    """What survives between turns.

    Deliberately not the whole trace: spans are for explaining a past run, this
    is for continuing one. Keeping them apart is what stops a checkpoint growing
    without bound.
    """

    model_config = ConfigDict(frozen=True)

    conversation_id: ConversationId
    customer_id: str
    messages: tuple[Message, ...] = ()
    recent: tuple[TurnNote, ...] = ()
    """The last few turn outcomes, newest last, capped at `RECENT_TURNS`.

    Added for Tier 2 escalation, which asks how the conversation is *going* —
    and nothing here recorded that. The messages hold what was said; these hold
    what happened, and no rule can be written over the first."""
    turn_count: int = 0
    """Every turn, not just the remembered ones. `len(recent)` stops at the cap
    and a rule about a long conversation needs the real number."""
    escalated_rules: tuple[str, ...] = ()
    """Which rules have already fetched a person for this conversation.

    The cooldown, in storage terms. A Tier 2 condition does not stop holding
    because an escalation lapsed, so without this the same rule would raise,
    lapse and raise again for as long as the customer kept talking."""
    pending_approval_id: str | None = None
    """Set when a turn ended in `NeedsApproval`. The next turn resumes from here
    rather than starting again — which is what "the approval returns, it does not
    block" means in storage terms."""
    pending_escalation_id: str | None = None
    """Set when a turn ended in `Escalated`. A person owns the conversation.

    Without this the escalation was not sticky: the agent said a colleague would
    take over and then answered the customer's next message itself, because
    nothing in what survives a turn recorded that the handoff had happened. One
    field, and it is the difference between a promise and a state.
    """

    def with_messages(self, *added: Message) -> Conversation:
        return self.model_copy(update={"messages": (*self.messages, *added)})

    def with_turn(self, note: TurnNote) -> Conversation:
        """Record what this turn did, dropping the oldest once the cap is hit."""
        return self.model_copy(
            update={
                "recent": (*self.recent, note)[-RECENT_TURNS:],
                "turn_count": self.turn_count + 1,
            }
        )

    def recording(self, result: TurnResult) -> Conversation:
        """Append what the customer was told, and remember an open approval.

        `Failed.detail` is deliberately not stored on the conversation: it is for
        the operator and lives on the span. A conversation is what the customer
        can be shown.
        """
        reply = getattr(result, "reply", None) or getattr(result, "customer_message", "")
        updated = self.with_messages(
            Message(role="assistant", content=reply, provenance="operator")
        )
        if isinstance(result, NeedsApproval):
            return updated.model_copy(update={"pending_approval_id": result.approval_id})
        # Only when there is a record to point at. An escalation with no
        # `ticket_id` is one no store accepted, and flagging the conversation
        # against a record that does not exist would hold it closed with nothing
        # able to reopen it.
        if isinstance(result, Escalated) and result.ticket_id is not None:
            fired = updated.escalated_rules
            if result.rule_id and result.rule_id not in fired:
                fired = (*fired, result.rule_id)
            return updated.model_copy(
                update={"pending_escalation_id": result.ticket_id, "escalated_rules": fired}
            )
        return updated

    def encode(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, raw: bytes) -> Conversation:
        return cls.model_validate_json(raw)


class InMemoryCheckpointStore:
    """Process-local. Correct within one run, gone at restart.

    Fine for tests and for a sealed simulation, where the process outlives the
    scenario by construction. Not fine where an approval may take a human an hour.
    """

    durable = False

    def __init__(self) -> None:
        self._runs: dict[str, bytes] = {}
        self._conversations: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def checkpoint(
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None:
        async with self._lock:
            self._runs[run_id] = state
            # The same bytes under both keys — F-006. The run index explains a
            # past turn; the conversation index is the only one a caller can
            # reach, because a customer holds a conversation id and never a run
            # id. One write, because two writes can disagree.
            self._conversations[conversation_id] = state

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        async with self._lock:
            return self._conversations.get(conversation_id)


class FileCheckpointStore:
    """Durable without infrastructure. Survives a restart; does not survive
    concurrent writers on different machines.

    Writes are atomic — a temporary file in the same directory, then `replace`,
    which is atomic on POSIX. A checkpoint half-written during a crash is worse
    than no checkpoint at all, because it resumes into a state that never
    existed.

    Postgres replaces this when there is more than one process, not before.
    """

    durable = True

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, run_id: RunId) -> Path:
        return self._dir / f"{run_id}.json"

    def _conversation_path(self, conversation_id: ConversationId) -> Path:
        return self._dir / f"conversation-{conversation_id}.json"

    async def checkpoint(
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None:
        async with self._lock:
            self._write(self._path(run_id), state)
            # Written second and separately rather than symlinked or indexed:
            # both are atomic replaces, so a crash between them leaves the run
            # record correct and the conversation pointer one turn stale, which
            # is recoverable. An index that could point at a half-written file
            # would not be.
            self._write(self._conversation_path(conversation_id), state)

    def _write(self, target: Path, state: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=self._dir, delete=False, suffix=".tmp") as handle:
            handle.write(state)
            temporary = Path(handle.name)
        temporary.replace(target)

    async def resume(self, run_id: RunId) -> bytes | None:
        return await self._read(self._path(run_id))

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        return await self._read(self._conversation_path(conversation_id))

    async def _read(self, target: Path) -> bytes | None:
        async with self._lock:
            if not target.exists():
                return None
            raw = target.read_bytes()
        try:
            json.loads(raw)
        except ValueError:
            return None
        return raw


__all__ = [
    "RECENT_TURNS",
    "Conversation",
    "FileCheckpointStore",
    "InMemoryCheckpointStore",
    "TurnNote",
]
