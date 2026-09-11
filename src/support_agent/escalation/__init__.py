"""Handing a conversation to a person.

L14 · P6 and P8, and the sibling of `approvals`. The split between them is the
one the import contract already forced once: this module is *policy* — what a
raise means, how long it stays open, what the customer is told — while the
durable substrate belongs to `state`. `PostgresEscalationStore` therefore lives
in `state.postgres`, exactly where `PostgresApprovalStore` does.

## What this fixes

Before it, `Escalate` produced a sentence and nothing else. The agent told a
customer *"let me pass you to a colleague"* — a promise no tool had confirmed,
which the system prompt three modules away explicitly forbids — and then, on the
next turn, answered them itself, because nothing recorded that a handoff had
happened. `ticket_id` had been declared on `Escalated` since the beginning and
was never once set.

## Why an escalation expires

`approvals` has an expiry because a decision can arrive too late to act on. This
has one because **nobody may come at all**, and a conversation held open against
a colleague who never appeared is worse than one that lapses and says so. There
is no reviewer surface yet — that is step 4 — so without expiry the first version
of this module would silently brick every escalated conversation.

The lapse is therefore a designed path, not a failure: the conversation returns
to the agent, the customer is told the truth, and the record survives as
evidence that nobody answered. That last part is the point. *Failing to act is
the failure mode with no evidence*, and this is the one place we can leave some.
"""

from __future__ import annotations

from support_agent.escalation.capacity import (
    Capacity,
)
from support_agent.escalation.store import (
    InMemoryEscalationStore,
)
from support_agent.escalation.wording import (
    CLOSED_REPLY,
    LAPSED_REPLY,
    NO_DESK_REPLY,
    QUEUED_REPLY,
    RAISED_REPLY,
    WAITING_REPLY,
    humanise,
)
from support_agent.escalation.workflow import (
    DEFAULT_TTL_S,
    EscalationError,
    lapse,
    new_escalation_id,
    raise_for,
    resolve,
    sweep,
)

__all__ = [
    "CLOSED_REPLY",
    "DEFAULT_TTL_S",
    "QUEUED_REPLY",
    "Capacity",
    "EscalationError",
    "LAPSED_REPLY",
    "NO_DESK_REPLY",
    "RAISED_REPLY",
    "WAITING_REPLY",
    "InMemoryEscalationStore",
    "humanise",
    "lapse",
    "new_escalation_id",
    "raise_for",
    "resolve",
    "sweep",
]
