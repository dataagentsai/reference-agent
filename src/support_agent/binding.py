"""Realisation this agent's world used to carry.

The authority an operation requires is two statements, and they belong in
different places: *which* operations are privileged is the agent
specification's — `issue_refund.authority` says a person decides — and what the
privilege is **called** belongs to whatever issues credentials. The world
carried both in an `x_binding` block until the binding spec existed to hold the
second, which is the dependency invariant the family's charter states: bindings
depend on specifications, never the reverse.

The map below is the same statement as `tool_runtime.x_scopes` in
`harness-profile.yaml`, and a test asserts they agree. Two statements of one
rule drift; one statement with a check does not.
"""

from __future__ import annotations

from agent_harness import identity as ident

SYSTEM_PROMPT = (
    "You are a customer support agent for a clothing retailer. "
    "Answer only from what the tools return. "
    "Never promise a delivery date, a refund amount or a policy exception that a "
    "tool has not confirmed. If you cannot do something, say so plainly. Asked "
    "about an order without its number, list their orders instead of asking."
)
"""What the model is told it is. This shop's words, so this file and not the
composition root, which only passes them on (prompt version v2, T-072)."""


SCOPES: dict[str, str] = {
    "cancel_order": ident.SCOPE_ORDERS_WRITE,
    "open_return_request": ident.SCOPE_RETURNS_WRITE,
    "change_address": ident.SCOPE_ORDERS_WRITE,
    "issue_refund": ident.SCOPE_REFUNDS_WRITE,
}
"""Operation to the scope a caller must hold. A read needs none: the order
system answers about the caller's own rows and nothing else (P-OWNERSHIP)."""


FRESH_FOR_S = 30
"""How long a read of an order's status stays usable — AOAS `order.status`
`fresh_for: 30s`, carried here because the *number* is the specification's and
acting on it is the harness's (AHC-0107).

It is a single number rather than a map because this agent has one entity whose
row another system writes while a conversation is open. A second such entity
would make this a map, and the shape of that map is the thing to get right then
rather than now.
"""


__all__ = ["FRESH_FOR_S", "SCOPES"]
