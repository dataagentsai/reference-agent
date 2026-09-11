"""Where a person can intervene.

L14 · P6 and P8. The queue, the decision, and resumption.

**The approval returns; it never blocks.** L14's own question is what the system
does while it waits, and the answer is: it checkpoints and hands back a typed
`NeedsApproval`. Nothing is held open, nothing is promised to the customer that
the approver has not granted.

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

So the decision and its audit trail are business state and live in `agent_state`.
How a resumption is *signalled* — a poll, a webhook, elicitation on a different
connection — remains a detail that can change without touching this module.

### Privilege separation

`CUSTOMER_SCOPES` deliberately excludes `refunds:write`, so an agent acting as the
customer cannot refund at all. That is not an oversight to work around: the
elevated scope exists **only** inside `granted_identity`, which refuses to mint it
without a granted, unexpired approval. The gate is not the only control; it is the
second one, and this is the first.
"""

from __future__ import annotations

from support_agent.approvals.policy import (
    REFUND_ACTION,
    Policy,
    requires_approval,
)
from support_agent.approvals.refund import (
    REFUND_WAIT_REPLY,
    REQUEST_REFUND,
    REQUEST_REFUND_SPEC,
    RefundRequested,
    refund_tool,
)
from support_agent.approvals.store import (
    InMemoryApprovalStore,
)
from support_agent.approvals.workflow import (
    ApprovalError,
    decide,
    granted_identity,
    is_executable,
    request,
    stored_key,
)

__all__ = [
    "REFUND_ACTION",
    "REFUND_WAIT_REPLY",
    "REQUEST_REFUND",
    "REQUEST_REFUND_SPEC",
    "RefundRequested",
    "refund_tool",
    "ApprovalError",
    "InMemoryApprovalStore",
    "Policy",
    "decide",
    "granted_identity",
    "is_executable",
    "request",
    "requires_approval",
    "stored_key",
]
