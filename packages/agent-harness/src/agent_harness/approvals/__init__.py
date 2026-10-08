"""Where a person can intervene.

L14 · P6 and P8. The queue, the decision, and resumption.

**The approval returns; it never blocks the turn.** L14's own question is what
the system does while it waits, and the answer is: the turn checkpoints and hands
back a typed `NeedsApproval`, and the wait itself is a Temporal workflow that
outlives the process (T-028). Nothing is promised to the customer that the
approver has not granted.

### Why not MCP elicitation

`InputRequiredResult` with `elicitation/create` is a protocol-native round trip
and was evaluated for this. It is the wrong shape here for two reasons, and both
are about who and how long:

*The approver is not the caller.* Elicitation asks the party on the other end of
the connection. A refund is authorised by an operations reviewer, not by the
customer whose refund it is.

*The wait is asynchronous and long.* Elicitation resolves inside one tool call. An
approval may take an hour, span a process restart, and be decided by someone who
was not present when it was requested.

So the decision and its audit trail are business state, and live in the
approval workflow's history.
How a resumption is *signalled* — a poll, a webhook, elicitation on a different
connection — remains a detail that can change without touching this module.

### Privilege separation

`CUSTOMER_SCOPES` deliberately excludes `refunds:write`, so an agent acting as the
customer cannot refund at all. That is not an oversight to work around: the
elevated scope exists **only** inside `granted_identity`, which refuses to mint it
without a granted, unexpired approval, and only the approvals worker calls it.
The agent is handed `TemporalApprovals`, which can ask and read; a reviewer is
handed `ApprovalDesk`, which can decide. The gate is not the only control; it is
the second one, and this is the first.
"""

from __future__ import annotations

from agent_harness.approvals.desk import (
    TASK_QUEUE,
    WORKFLOWS,
    ApprovalDesk,
    TemporalApprovals,
    approval_id,
    connect_temporal,
    metrics_runtime,
    worker,
)
from agent_harness.approvals.durable import REMIND
from agent_harness.approvals.notify import Nobody, Notifier, Reminders, message
from agent_harness.approvals.workflow import (
    ApprovalError,
    ApprovalTerms,
    Terms,
    carry_out,
    granted_identity,
    is_executable,
    moved,
    refusal,
    stored_key,
)
from agent_harness.contracts import Approval, ApprovalState

__all__ = [
    "TASK_QUEUE",
    "WORKFLOWS",
    "Approval",
    "REMIND",
    "ApprovalDesk",
    "Nobody",
    "message",
    "Notifier",
    "Reminders",
    "ApprovalError",
    "ApprovalState",
    "ApprovalTerms",
    "TemporalApprovals",
    "Terms",
    "approval_id",
    "carry_out",
    "connect_temporal",
    "metrics_runtime",
    "granted_identity",
    "is_executable",
    "moved",
    "refusal",
    "stored_key",
    "worker",
]
