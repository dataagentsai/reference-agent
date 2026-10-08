"""This agent's own spans, declared into the harness's span contract — and the
names they carry.

The harness declares the spans it opens (`agent_harness.telemetry.contract`).
These six are opened by this shop's own modules — the router, the deterministic
answers, the opening, the refund flow and the promise gate — so they are this
agent's to declare. Declared once, at import of the package root, before any
span can be checked.

The names are this agent's too (T-099): the instrumentation scope every one of
its spans and numbers carries, and the `service.name` its provider is built
with. They were the library's constants until the harness was a library; set
here to the values they had, so no trace, dashboard or stored scope changes.
"""

from __future__ import annotations

from agent_harness import telemetry
from agent_harness.telemetry.contract import SpanSpec, declare
from agent_harness.telemetry.names import ROUTE_KIND, ROUTE_REASON, TENANT

SPANS: dict[str, SpanSpec] = {
    "agent.route": SpanSpec(
        required=frozenset({ROUTE_KIND, ROUTE_REASON, "agent.router.rules_version"})
    ),
    "agent.direct": SpanSpec(required=frozenset({"agent.handler"})),
    "agent.opening": SpanSpec(
        # P-OPEN. What was shown: how many orders (-1 when the order system would
        # not say) and how much work in flight. No model span may sit under it.
        required=frozenset({TENANT, "agent.opening.orders", "agent.opening.in_flight"}),
    ),
    "agent.approval.resume": SpanSpec(required=frozenset({"agent.approval.id"})),
    # Granted by the policy and carried out in the same call — a refund that is
    # owed and within the limit (F-014). The decide span inside it names no person.
    "agent.approval.carry_out": SpanSpec(required=frozenset({"agent.approval.id"})),
    # AHC-0106. Emitted only when a completed reply promised something nothing
    # was doing — so a rate here is the rate at which the model writes cheques
    # this agent cannot cash, and `kind` says which cheque.
    "agent.promise.unbacked": SpanSpec(
        required=frozenset({"agent.promise.kind", "agent.promise.rules_version"})
    ),
}

SCOPE = "support_agent"
SERVICE = "support-agent"

telemetry.identify(scope=SCOPE, service=SERVICE)
declare(SPANS)

__all__ = ["SCOPE", "SERVICE", "SPANS"]
