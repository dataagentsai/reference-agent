"""A tool server that enforces the policy, so the agent does not have to.

This is the business world the agent talks to — and the seed of what AgentTwin
will later project from a declared world file rather than hard-code.

**The agent does not know the return window.** Eligibility lives here, on the
server, because that is where it lives in reality: a support agent that encoded
the cancellation rule would be a second copy of a policy that changes without
telling it, and the copy would be the one customers were held to.

So the golden set below exercises the *system* — agent plus server — rather than
a rule the agent was handed. That is the difference between testing a lookup
table and testing a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent.contracts import OrderStatus, SideEffectClass
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT

RETURN_WINDOW_DAYS = 30
DAMAGE_WINDOW_DAYS = 7

CANCELLABLE = {OrderStatus.PENDING, OrderStatus.CONFIRMED}
"""P-CANCEL. Once picked, the parcel is moving and cancellation is a return."""

ADDRESS_CHANGEABLE = {OrderStatus.PENDING}
"""P-ADDRESS. After that the carrier owns the address."""


class Outcome(BaseModel):
    allowed: bool
    reason: str
    order_id: str
    status: str


class OrderOut(BaseModel):
    order_id: str
    status: str
    days_since_delivery: int
    final_sale: bool


@dataclass
class World:
    """Seeded order state, and a record of what actually happened.

    `effects` is the oracle. A scenario asserts against what changed in the
    world, not against what the agent said about it — which is the one kind of
    assertion a transcript-grading eval cannot make.
    """

    orders: dict[str, dict] = field(default_factory=dict)
    effects: list[tuple[str, str]] = field(default_factory=list)

    def seed(
        self,
        order_id: str,
        status: OrderStatus,
        *,
        days_since_delivery: int = 0,
        final_sale: bool = False,
    ) -> None:
        self.orders[order_id] = {
            "status": status.value,
            "days_since_delivery": days_since_delivery,
            "final_sale": final_sale,
        }

    def status_of(self, order_id: str) -> OrderStatus | None:
        row = self.orders.get(order_id)
        return OrderStatus(row["status"]) if row else None

    def count(self, action: str) -> int:
        return sum(1 for a, _ in self.effects if a == action)


def build(world: World) -> MCPServer:
    srv = MCPServer("ecom")

    def _known(order_id: str) -> dict | None:
        return world.orders.get(order_id)

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order's current status."""
        row = _known(order_id)
        if row is None:
            raise ValueError(f"no order {order_id}")
        return OrderOut(order_id=order_id, **row)

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_ORDERS_WRITE,
        }
    )
    def cancel_order(order_id: str) -> Outcome:
        """Cancel an order. Only possible before it has been picked."""
        row = _known(order_id)
        if row is None:
            raise ValueError(f"no order {order_id}")
        status = OrderStatus(row["status"])
        if status not in CANCELLABLE:
            return Outcome(
                allowed=False,
                reason=f"an order that is {status.value} can no longer be cancelled",
                order_id=order_id,
                status=status.value,
            )
        row["status"] = OrderStatus.CANCELLED.value
        world.effects.append(("cancel_order", order_id))
        return Outcome(allowed=True, reason="cancelled", order_id=order_id, status="cancelled")

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.REVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_RETURNS_WRITE,
        }
    )
    def open_return_request(order_id: str) -> Outcome:
        """Open a return. Delivered orders only, inside the window, not final sale."""
        row = _known(order_id)
        if row is None:
            raise ValueError(f"no order {order_id}")
        status = OrderStatus(row["status"])
        if status is not OrderStatus.DELIVERED:
            return Outcome(
                allowed=False,
                reason=f"only a delivered order can be returned; this one is {status.value}",
                order_id=order_id,
                status=status.value,
            )
        if row["days_since_delivery"] > RETURN_WINDOW_DAYS:
            return Outcome(
                allowed=False,
                reason=f"the {RETURN_WINDOW_DAYS}-day return window has closed",
                order_id=order_id,
                status=status.value,
            )
        if row["final_sale"]:
            # No exception path. A window can be argued about; a final-sale flag
            # cannot, and a rule with one documented exception grows more.
            return Outcome(
                allowed=False,
                reason="final sale items cannot be returned",
                order_id=order_id,
                status=status.value,
            )
        world.effects.append(("open_return_request", order_id))
        return Outcome(allowed=True, reason="return opened", order_id=order_id, status=status.value)

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.REVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_ORDERS_WRITE,
        }
    )
    def change_address(order_id: str) -> Outcome:
        """Change the delivery address. Only while the order is still pending."""
        row = _known(order_id)
        if row is None:
            raise ValueError(f"no order {order_id}")
        status = OrderStatus(row["status"])
        if status not in ADDRESS_CHANGEABLE:
            return Outcome(
                allowed=False,
                reason=f"the carrier owns the address once an order is {status.value}",
                order_id=order_id,
                status=status.value,
            )
        world.effects.append(("change_address", order_id))
        return Outcome(
            allowed=True, reason="address updated", order_id=order_id, status=status.value
        )

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_REFUNDS_WRITE,
        }
    )
    def issue_refund(order_id: str, amount: str) -> Outcome:
        """Issue a refund. Requires the elevated scope a granted approval mints."""
        world.effects.append(("issue_refund", order_id))
        return Outcome(allowed=True, reason="refunded", order_id=order_id, status="refunded")

    return srv


__all__ = [
    "ADDRESS_CHANGEABLE",
    "CANCELLABLE",
    "DAMAGE_WINDOW_DAYS",
    "RETURN_WINDOW_DAYS",
    "Outcome",
    "World",
    "build",
]
