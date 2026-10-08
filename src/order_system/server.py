"""The store's own MCP server: the surface the agent actually talks to (T-017).

The same six tools the projected world offers, with the same names, arguments,
scopes and side-effect classes — and behind them a real store. Only
`AGENT_MCP_BASE_URL` changes on the agent's side, which is what
`ResolutionMode.real` was declared for and has never until now meant anything.

**Every call passes the far end's check first** (`order_system.authoriser`): a
token addressed to this system, and, for anything the token does not already
allow, the approval that covers the call. Whose orders these are comes from the
verified session and never from an argument, so `list_orders` is filtered by the
token rather than by what the caller asked for.

**And every write is recognised on its key.** The agent's ledger knows only what
it saw succeed; when the effect lands and the reply is lost, the system that
applied it is the only one that can recognise the retry (F-017). Saleor has no
idempotency keys, so this server keeps them.

The metadata keys are declared here rather than imported from the agent's
transport, for the reason the authoriser's already are: the far end does not
learn from the caller where the caller put things.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from agent_harness.identity import Issuer
from order_system import APPROVAL_META, IDEMPOTENCY_META, SESSION_META, authoriser
from order_system.store import Store
from support_agent.contracts import ApprovalRecords

META_SIDE_EFFECT = "side_effect"
META_ENTITY = "entity"
"""Which kind of row a tool is about. Every operation in this AOAS is on
`order`; declaring it anyway is what lets a caller pick the right reader
when a second entity arrives (T-061)."""
ORDER = "order"
META_REQUIRED_SCOPE = "required_scope"
"""Where a tool says what it costs to repeat and what it requires. Declared
here rather than imported from the agent's transport, for the reason the
authoriser's keys already are — but the *names* are the protocol's, so a store
and an agent that never share code still agree."""

READ = "read"
REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"

SCOPES = {
    "cancel_order": "orders:write",
    "change_address": "orders:write",
    "open_return_request": "returns:write",
    "issue_refund": "refunds:write",
}
"""What this system requires of a caller, stated by this system.

The agent's binding has its own copy and filters its tool surface with it. Two
independent statements, and if they disagree the agent offers a tool that is
refused here — which is the failure worth having, because the other way round is
a tool that is offered and *works* when it should not.
"""


@dataclass
class Answered:
    """What this store has already done, by the key it was asked under.

    In memory, and marked so: it answers for one process, which is what the
    projected world's `answered` does too. A deployment keeps this beside the
    orders, in the same transaction as the effect, or the retry it is for can
    still land twice.
    """

    durable: bool = False
    seen: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, key: str | None) -> dict[str, Any] | None:
        return copy.deepcopy(self.seen[key]) if key is not None and key in self.seen else None

    def put(self, key: str | None, answer: dict[str, Any]) -> dict[str, Any]:
        if key is not None:
            self.seen[key] = copy.deepcopy(answer)
        return answer


def _meta(ctx: Context | None) -> dict[str, Any]:
    """The call's metadata, or empty — never a guess."""
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except (AttributeError, ValueError):
        return {}
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return meta
    return dict(getattr(meta, "model_extra", None) or {})


def _key(meta: dict[str, Any]) -> str | None:
    key = meta.get(IDEMPOTENCY_META)
    return key if isinstance(key, str) and key else None


def _reads(server: MCPServer, store: Store, *, acting: Any, theirs: Any) -> None:
    """The two reads. Both answer for the session's customer and nobody else."""

    @server.tool(meta={META_SIDE_EFFECT: READ, META_ENTITY: ORDER}, structured_output=True)
    async def get_order(id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Look up an order's current status."""
        customer = await acting("get_order", {"id": id}, ctx)
        found = await theirs(id, customer)
        return found if found is not None else {"found": False, "id": id}

    @server.tool(meta={META_SIDE_EFFECT: READ, META_ENTITY: ORDER}, structured_output=True)
    async def list_orders(ctx: Context | None = None) -> dict[str, Any]:
        """List this customer's orders, newest first."""
        customer = await acting("list_orders", {}, ctx)
        found = await store.list_orders(customer)
        return {
            "found": True,
            "items": [row for row in found["items"] if row.get("customer_id") == customer],
        }


def _writes(server: MCPServer, store: Store, *, acts: Any) -> None:
    """The four writes, each behind the same check, ownership test and key."""

    @server.tool(
        meta={
            META_SIDE_EFFECT: IRREVERSIBLE,
            META_ENTITY: ORDER,
            META_REQUIRED_SCOPE: SCOPES["cancel_order"],
        },
        structured_output=True,
    )
    async def cancel_order(id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Cancel an order. Only possible before it has been picked."""
        return await acts("cancel_order", store.cancel_order)(id, ctx, {})

    @server.tool(
        meta={
            META_SIDE_EFFECT: REVERSIBLE,
            META_ENTITY: ORDER,
            META_REQUIRED_SCOPE: SCOPES["open_return_request"],
        },
        structured_output=True,
    )
    async def open_return_request(id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Open a return. Delivered orders only, inside the window, not final sale."""
        return await acts("open_return_request", store.open_return_request)(id, ctx, {})

    @server.tool(
        meta={
            META_SIDE_EFFECT: REVERSIBLE,
            META_ENTITY: ORDER,
            META_REQUIRED_SCOPE: SCOPES["change_address"],
        },
        structured_output=True,
    )
    async def change_address(id: str, address: str, ctx: Context | None = None) -> dict[str, Any]:
        """Change the delivery address. Only while the order is still pending."""
        return await acts("change_address", store.change_address)(id, ctx, {"address": address})

    @server.tool(
        meta={
            META_SIDE_EFFECT: IRREVERSIBLE,
            META_ENTITY: ORDER,
            META_REQUIRED_SCOPE: SCOPES["issue_refund"],
        },
        structured_output=True,
    )
    async def issue_refund(id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Issue a refund. Requires the elevated scope a granted approval mints."""
        return await acts("issue_refund", store.issue_refund)(id, ctx, {})


def build(
    store: Store,
    *,
    issuer: Issuer,
    clock: Callable[[], int],
    approvals: ApprovalRecords,
    approvals_party: str | None = None,
    answered: Answered | None = None,
) -> MCPServer:
    """The store, as an MCP server that decides for itself who is calling."""
    server = MCPServer("ecom")
    ledger = answered or Answered()
    check = authoriser(
        issuer=issuer,
        approvals=approvals,
        required_scopes=SCOPES,
        clock=clock,
        approvals_party=approvals_party,
    )

    async def acting(operation: str, arguments: dict[str, Any], ctx: Context | None) -> str:
        meta = _meta(ctx)
        decided = await check(operation, arguments, meta)
        return str(decided["customer_id"])

    async def theirs(external: str, customer_id: str) -> dict[str, Any] | None:
        """The order, if it is this customer's. Not theirs is answered exactly
        as not there (F-016): a refusal would tell a stranger their guess was
        right, and what was behind it."""
        found = await store.get_order(external)
        if not found.get("found") or found.get("customer_id") != customer_id:
            return None
        return found

    def acts(
        name: str, run: Callable[..., Awaitable[dict[str, Any]]]
    ) -> Callable[[str, Context | None, dict[str, Any]], Awaitable[dict[str, Any]]]:
        """One write: checked, owned, recognised on its key, then done."""

        async def acting_on(
            external: str, ctx: Context | None, rest: dict[str, Any]
        ) -> dict[str, Any]:
            customer = await acting(name, {"id": external, **rest}, ctx)
            if await theirs(external, customer) is None:
                return {"allowed": False, "reason": "no such order", "id": external}
            key = _key(_meta(ctx))
            seen = ledger.get(key)
            if seen is not None:
                return seen
            return ledger.put(key, await run(external, **rest))

        return acting_on

    _reads(server, store, acting=acting, theirs=theirs)
    _writes(server, store, acts=acts)
    return server


__all__ = [
    "APPROVAL_META",
    "IDEMPOTENCY_META",
    "IRREVERSIBLE",
    "META_ENTITY",
    "META_REQUIRED_SCOPE",
    "META_SIDE_EFFECT",
    "READ",
    "REVERSIBLE",
    "SCOPES",
    "SESSION_META",
    "Answered",
    "build",
]
