"""Business vocabulary: what the agent can be asked, and the states an order passes through.

L6. What an action costs to repeat (`SideEffectClass`) and why a loop stopped
(`TerminationReason`) are the harness's, in `agent_harness.contracts.kinds`, and
re-exported here for every caller that learned them from this module. A
read-only agent leaves IRREVERSIBLE empty, which is precisely why customer
support was chosen over a cost analyst as the reference.
"""

from __future__ import annotations

from enum import StrEnum

from agent_harness.contracts.kinds import SideEffectClass, TerminationReason


class Intent(StrEnum):
    """What the customer is asking for. Classified deterministically, per turn."""

    ORDER_STATUS = "order_status"
    CANCEL_ORDER = "cancel_order"
    RETURN_REQUEST = "return_request"
    EXCHANGE_REQUEST = "exchange_request"
    REFUND_STATUS = "refund_status"
    REFUND_REQUEST = "refund_request"
    ADDRESS_CHANGE = "address_change"
    DAMAGED_ITEM = "damaged_item"
    POLICY_QUESTION = "policy_question"
    ESCALATE = "escalate"
    OUT_OF_SCOPE = "out_of_scope"


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


__all__ = ["Intent", "OrderStatus", "SideEffectClass", "TerminationReason"]
