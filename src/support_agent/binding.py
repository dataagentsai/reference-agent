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

from support_agent import identity as ident

SCOPES: dict[str, str] = {
    "cancel_order": ident.SCOPE_ORDERS_WRITE,
    "open_return_request": ident.SCOPE_RETURNS_WRITE,
    "change_address": ident.SCOPE_ORDERS_WRITE,
    "issue_refund": ident.SCOPE_REFUNDS_WRITE,
}
"""Operation to the scope a caller must hold. A read needs none: the order
system answers about the caller's own rows and nothing else (P-OWNERSHIP)."""


__all__ = ["SCOPES"]
