"""Business vocabulary: what the agent can be asked, and what its actions cost.

L6. The `SideEffectClass` here is the sharpest thing in the functional spec —
it is what turns L10 idempotency and L14 approval gates from theory into code.
A read-only agent leaves IRREVERSIBLE empty, which is precisely why customer
support was chosen over a cost analyst as the reference.
"""

from __future__ import annotations

from enum import StrEnum


class Intent(StrEnum):
    """What the customer is asking for. Classified deterministically, per turn."""

    ORDER_STATUS = "order_status"
    CANCEL_ORDER = "cancel_order"
    RETURN_REQUEST = "return_request"
    EXCHANGE_REQUEST = "exchange_request"
    REFUND_STATUS = "refund_status"
    ADDRESS_CHANGE = "address_change"
    DAMAGED_ITEM = "damaged_item"
    POLICY_QUESTION = "policy_question"
    ESCALATE = "escalate"
    OUT_OF_SCOPE = "out_of_scope"


class SideEffectClass(StrEnum):
    """What repeating an action costs.

    READ         — repeating is free and invisible.
    REVERSIBLE   — repeating leaves a trace that can be undone.
    IRREVERSIBLE — repeating charges someone twice. Requires an idempotency key
                   at the tool boundary, and may require human approval.
    """

    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"

    @property
    def requires_idempotency_key(self) -> bool:
        return self is not SideEffectClass.READ


class OrderStatus(StrEnum):
    """The spine of every eligibility rule.

    pending → confirmed → picked → shipped → out_for_delivery → delivered
                                                                    ↓
                                            returned / refunded / cancelled
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PICKED = "picked"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"
    REFUNDED = "refunded"


class TerminationReason(StrEnum):
    """Why the loop stopped. Never absent — AAC-0055 requires hard termination
    under every condition, and an unexplained stop is indistinguishable from a
    hang.
    """

    GOAL_REACHED = "goal_reached"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    COST_CEILING_REACHED = "cost_ceiling_reached"
    DEADLINE_REACHED = "deadline_reached"
    """Wall-clock ran out (AHC-0096). Its own value since generation run 1: it
    was reported as a step stop, and the two call for different fixes."""
    OUTPUT_LENGTH_REACHED = "output_length_reached"
    """The model stopped because its output budget ran out (AHC-0025), so what
    it wrote is incomplete. Its own value since generation run 2: sending a
    clipped answer as if it were whole is the failure it names."""
    OSCILLATION_DETECTED = "oscillation_detected"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_HUMAN = "awaiting_human"
    """A person owns the conversation now.

    Distinct from `AWAITING_APPROVAL`: an approval is a decision about one action
    the agent proposed, and the agent resumes afterwards. This is the whole
    conversation changing hands. Both are "not finished", which is why both
    answer 202 rather than 200 — but a dashboard that could not tell them apart
    would report a handoff as a stalled refund.
    """
    REFUSED = "refused"
    UNRECOVERABLE_ERROR = "unrecoverable_error"
