"""When a person owns the work.

L14 · L6. The record that outlives the turn that raised it.

`Approval` is the sibling of this and lives in `contracts.tools`, because it is
coupled to the local tool that requests one. Nothing requests an escalation
through a tool — the router decides it before the model is reached at all — so
this record has no tool to sit beside.

**The record is the whole point.** Before it existed, an escalation was a reply
string and a span attribute: the agent told a customer a colleague would take
over and then, on the very next turn, answered them itself. Nothing was written
down, so nothing could be picked up, counted, or found again.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EscalationState(StrEnum):
    """Deliberately small for now.

    `assigned` and `in_progress` belong to the reviewer surface and are not
    modelled until something can set them — a state no code can reach is a state
    that lies about what the system does.
    """

    QUEUED = "queued"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    """Nobody claimed it before `expires_at`.

    A terminal state rather than an error: an escalation that is never answered
    is the common case in any real operations queue, and one that silently held
    a conversation open forever would be worse than one that lapses and says so.
    """


class EscalationOutcome(StrEnum):
    """How it ended, recorded by whoever ended it.

    This is the ground truth that makes over- and under-escalation *measurable*
    rather than arguable — AAC-0020's over-refusal rate and AAC-0088's
    false-positive half both need a label, and the person closing the ticket is
    the only one who can supply it. Recorded from the first version so the data
    exists when the analysis is built, not after.
    """

    RESOLVED = "resolved"
    AGENT_COULD_HAVE = "agent_could_have"
    """Over-escalation, admitted by the human who picked it up."""
    MISROUTED = "misrouted"
    CUSTOMER_GONE = "customer_gone"


class Escalation(BaseModel):
    """A human owes this conversation an answer.

    Agent-owned state — it lives in `agent_state`, never in the simulated world,
    for the same reason approvals do: an oracle that can be faked is not one.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """The customer-facing handle. Said out loud in the reply, so it is short and
    readable rather than a bare uuid — a person reads this back over the phone."""

    conversation_id: str
    run_id: str
    """The turn that raised it. A conversation has many runs; this names the one
    that decided, so the decision can be found in the trace."""
    customer_id: str

    tier: int = 1
    """Which class of rule fired. 1 = the customer asked, or a hard policy class.
    2 and 3 exist in the design and have nothing to set them yet."""
    rule_id: str
    """*Which* rule, not just which tier. Slicing outcomes by this is the only
    way to find the rule that produces escalations humans say were unnecessary."""
    rules_version: str
    reason: str

    state: EscalationState = EscalationState.QUEUED
    created_at: int
    expires_at: int
    """Borrowed from `Approval`, and load-bearing for a different reason.

    An approval expiring means a decision arrived too late to act on. This
    expiring means *nobody came*, and the conversation must be handed back to the
    agent rather than held open against a colleague who never appeared. Without
    it, step 1 of the rebuild would brick every escalated conversation until the
    reviewer surface exists.
    """

    resolved_at: int | None = None
    outcome: EscalationOutcome | None = None
    outcome_by: str | None = None
    outcome_note: str | None = None

    @property
    def open(self) -> bool:
        return self.state is EscalationState.QUEUED

    def lapsed(self, now: int) -> bool:
        return self.open and now >= self.expires_at


__all__ = ["Escalation", "EscalationOutcome", "EscalationState"]
