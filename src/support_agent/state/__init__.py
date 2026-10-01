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
import time

from pydantic import BaseModel, ConfigDict

from support_agent.contracts import (
    Agentic,
    Clock,
    ConversationId,
    Direct,
    Escalated,
    Message,
    NeedsApproval,
    Route,
    RunId,
    StoredSession,
    TurnResult,
)
from support_agent.state.facts import Facts
from support_agent.state.file import FileCheckpointStore

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

        The intent is what the router **classified**, never a guess: a `Direct`
        route's intent, or an `Agentic` route's single candidate — one match that
        went to the loop for another reason, most often a request with no order
        id in it. Two candidates or none is an unclassified turn and stays
        `None`, because a rule counting those would be counting nothing anybody
        decided. Until F-025 only `Direct` counted, and a customer who asked the
        same thing three times without ever quoting an order id never reached the
        `repeated-intent` threshold.
        """
        return cls(
            route=decision.kind,
            result=result.kind,
            intent=_classified(decision),
            termination=getattr(result, "termination", None),
        )


def _classified(decision: Route) -> str | None:
    """The intent the router settled on, or `None` when it settled on none."""
    if isinstance(decision, Direct):
        return decision.intent.value
    if isinstance(decision, Agentic) and len(decision.candidate_intents) == 1:
        return decision.candidate_intents[0].value
    return None


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
    """Which rules have fetched a person since the agent last took the
    conversation back — from its start, or from the latest return.

    The cooldown, in storage terms (`P-ESC-ONCE`). A Tier 2 condition does not
    stop holding because an escalation lapsed, so without this the same rule
    would raise, lapse and raise again for as long as the customer kept talking.
    Emptied by `returned`, because what happens after a colleague hands the
    conversation back is new evidence; `escalations_raised` is what stops a
    loop, and it is never emptied. Which rule raised each escalation stays on
    the escalation's own record."""
    returned_at_turn: int = 0
    """`turn_count` when the conversation last came back to the agent: a
    colleague closed the escalation, it lapsed, or its record was gone.

    `P-ESC-FRESH`. The facts the condition rules read count only turns after
    this, so two failures before a handback and one after are not two in a row.
    `turn_count` and `recent` themselves are untouched — every other reader
    keeps the conversation's whole history."""
    escalations_raised: int = 0
    """How many references this conversation has been given.

    Deliberately **not** `len(escalated_rules)`, which is what the cap used to
    read: that counts *rules* and the cap is about *escalations* (F-034). One
    counter answering two statements meant a customer who asked for a person
    four times got four references, because it was the same rule every time —
    `P-ESC-ONCE` is the per-rule cooldown above, and this is `P-ESC-CAP`."""
    facts: Facts = Facts()
    """AHC-0108 — what this work is about, written as it happens.

    Beside `messages` rather than derived from them: everything here was put
    there by the harness at the moment it did something, so it survives a
    reduction of the transcript and cannot inherit the model's phrasing."""
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

    @property
    def since_return(self) -> tuple[TurnNote, ...]:
        """The remembered turns since the agent last took the conversation
        back (`P-ESC-FRESH`), newest last. All of `recent` when it never left."""
        if not self.returned_at_turn:
            return self.recent
        held = self.turn_count - self.returned_at_turn
        return self.recent[-held:] if held > 0 else ()

    def returned(self) -> Conversation:
        """The conversation comes back to the agent — `P-ESC-FRESH`, `P-ESC-ONCE`.

        Whoever finished it: a colleague who resolved or closed the escalation,
        the lapse when nobody came, or a record that is gone. The condition
        rules' count starts again from here, and each of them may fire once more.
        The cap (`escalations_raised`) is not reset — it is what bounds this.
        """
        return self.model_copy(
            update={
                "pending_escalation_id": None,
                "escalated_rules": (),
                "returned_at_turn": self.turn_count,
            }
        )

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
        # An escalation always has a record to point at — the type requires one.
        if isinstance(result, Escalated):
            fired = updated.escalated_rules
            raised = updated.escalations_raised + 1
            if result.rule_id and result.rule_id not in fired:
                fired = (*fired, result.rule_id)
            return updated.model_copy(
                update={
                    "pending_escalation_id": result.ticket_id,
                    "escalated_rules": fired,
                    "escalations_raised": raised,
                }
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

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._runs: dict[str, bytes] = {}
        self._conversations: dict[str, bytes] = {}
        self._whose: dict[str, tuple[str, ConversationId]] = {}
        self._written: dict[str, int] = {}
        """When each run and each conversation was last written — Q-RETENTION.
        Keyed like the two indexes, which cannot collide: ids are minted with
        different prefixes."""
        self._clock: Clock = clock or _wall
        self._lock = asyncio.Lock()

    async def checkpoint(
        self,
        run_id: RunId,
        state: bytes,
        *,
        conversation_id: ConversationId,
        customer_id: str = "",
    ) -> None:
        async with self._lock:
            self._runs[run_id] = state
            # The same bytes under both keys — F-006. The run index explains a
            # past turn; the conversation index is the only one a caller can
            # reach, because a customer holds a conversation id and never a run
            # id. One write, because two writes can disagree.
            self._conversations[conversation_id] = state
            # Whose, kept beside rather than inside — F-056. The state is the
            # only other place it appears, and erasure would have to decode
            # every turn ever stored to find one person's.
            self._whose[run_id] = (customer_id, conversation_id)
            written = self._clock()
            self._written[run_id] = self._written[conversation_id] = written

    async def expire(self, before: int) -> int:
        """Runs and conversations last written before `before` — Q-RETENTION."""
        async with self._lock:
            aged = {k for k, at in self._written.items() if at < before}
            runs = [r for r in self._runs if r in aged]
            for key in aged:
                self._runs.pop(key, None)
                self._conversations.pop(key, None)
                self._whose.pop(key, None)
                del self._written[key]
            return len(runs)

    async def forget(self, customer_id: str) -> tuple[RunId, ...]:
        """Their runs and conversations go; what is returned is the runs.

        An empty `customer_id` matches nothing, deliberately. It is what an
        unattributed checkpoint carries, and a blank request that erased all of
        them would be the worst possible failure of this method.
        """
        if not customer_id:
            return ()
        async with self._lock:
            runs = tuple(r for r, (c, _) in self._whose.items() if c == customer_id)
            for run in runs:
                _, conversation_id = self._whose.pop(run)
                self._runs.pop(run, None)
                self._conversations.pop(conversation_id, None)
                self._written.pop(run, None)
                self._written.pop(conversation_id, None)
            return tuple(RunId(r) for r in runs)

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        async with self._lock:
            return self._conversations.get(conversation_id)


class InMemorySessionStore:
    """Stored sessions for one process: tests, and a demo nobody logs out of."""

    durable = False

    def __init__(self) -> None:
        self._sessions: dict[str, StoredSession] = {}

    async def put(self, session: StoredSession) -> None:
        self._sessions[session.subject] = session

    async def get(self, subject: str) -> StoredSession | None:
        return self._sessions.get(subject)

    async def delete(self, subject: str) -> None:
        self._sessions.pop(subject, None)

    async def expire(self, before: int) -> int:
        aged = [k for k, s in self._sessions.items() if s.updated_at < before]
        for subject in aged:
            del self._sessions[subject]
        return len(aged)


def _wall() -> int:
    """The default clock, and the only place this module reads the wall."""
    return int(time.time())


__all__ = [
    "RECENT_TURNS",
    "Conversation",
    "FileCheckpointStore",
    "InMemoryCheckpointStore",
    "InMemorySessionStore",
    "TurnNote",
]
