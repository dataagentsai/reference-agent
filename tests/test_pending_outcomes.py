"""What the customer is told about a refund that waited for a person.

Only a refund that was carried out may be told as paid. Every other final state
says what happened instead; a state added later (`withdrawn`, 20 Oct 2026)
falls to the failure reply, never through to the success one, which is what it
did before: the success reply was the last line, reached by any state the
branches above it did not name.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_harness.contracts import Approval, ApprovalState, Identity, RunId
from agent_harness.state import Conversation
from support_agent.entrypoint.pending import ApprovalFlow

PAID = "on its way"


class OneApproval:
    def __init__(self, state: ApprovalState) -> None:
        self.approval = Approval(
            id="apr_1",
            action="issue",
            reason="above the automatic limit",
            customer_id="C-1",
            idempotency_key="k",
            created_at=0,
            expires_at=1,
            decided=state is not ApprovalState.WAITING,
            state=state,
            result="withdrawn by ops-7: opened twice" if state is ApprovalState.WITHDRAWN else None,
        )

    async def get(self, approval_id: str) -> Approval | None:
        return self.approval


def told(state: ApprovalState) -> str:
    flow = ApprovalFlow(OneApproval(state))  # type: ignore[arg-type]
    conversation = Conversation(
        conversation_id="cnv_1", customer_id="C-1", pending_approval_id="apr_1"
    )
    resumed = asyncio.run(
        flow.resume(conversation, Identity(customer_id="C-1"), RunId("run_1"), None)  # type: ignore[arg-type]
    )
    assert resumed is not None
    result, _ = resumed
    return str(getattr(result, "reply", None) or getattr(result, "customer_message", ""))


# (state, words the reply must contain)
OUTCOMES = [
    (ApprovalState.DONE, PAID),
    (ApprovalState.REFUSED, "could not authorise"),
    (ApprovalState.EXPIRED, "no longer valid"),
    (ApprovalState.FAILED, "could not be"),
    (ApprovalState.WITHDRAWN, "withdrew"),
]


@pytest.mark.parametrize(("state", "says"), OUTCOMES, ids=[s.value for s, _ in OUTCOMES])
@pytest.mark.discharges("AHC-0080")
def test_each_outcome_is_told_as_what_happened(state: ApprovalState, says: str) -> None:
    assert says in told(state)


@pytest.mark.parametrize(
    "state",
    [s for s in ApprovalState if s not in (ApprovalState.DONE, ApprovalState.STALE)],
    ids=lambda s: s.value,
)
@pytest.mark.discharges("AHC-0080")
def test_only_a_carried_out_(state: ApprovalState) -> None:
    assert PAID not in told(state)
