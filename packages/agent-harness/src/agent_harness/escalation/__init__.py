"""Handing a conversation to a person.

L14 · P6 and P8, and the sibling of `approvals`. This module is *policy* — what
a raise means, how long it stays open, what the customer is told — and the wait
itself is a Temporal workflow (T-028), as an approval's is.

What that policy **is** comes from the AOAS `policies.escalation` block entire:
`on_request` and `on_condition` are the two tiers, and the statements are the
rules — P-ESC-ONCE, P-ESC-FRESH, P-ESC-CAP, P-ESC-TTL, P-ESC-LAPSE, P-ESC-TOLD, P-ESC-OWNS
and P-ESC-OUTCOME. This package is that block, executable. Nothing here decides
what an escalation means; it decides when the spec's answer applies.

## What is not here

What the customer is told — every reply about the desk — is the agent's own
wording, and so are the Tier 2 rules an agent fires on. The handoff
(`agent_harness.entrypoint.handoff`) is handed both.

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

from agent_harness.escalation.capacity import (
    Capacity,
)
from agent_harness.escalation.desk import (
    TASK_QUEUE,
    WORKFLOWS,
    EscalationDesk,
    TemporalEscalations,
    worker,
)
from agent_harness.escalation.workflow import (
    DEFAULT_TTL_S,
    EscalationError,
    new_escalation_id,
    outcome_of,
    refusal,
)

__all__ = [
    "DEFAULT_TTL_S",
    "TASK_QUEUE",
    "WORKFLOWS",
    "Capacity",
    "EscalationDesk",
    "EscalationError",
    "TemporalEscalations",
    "new_escalation_id",
    "outcome_of",
    "refusal",
    "worker",
]
