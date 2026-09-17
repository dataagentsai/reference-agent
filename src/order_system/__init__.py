"""The order system's side of a call: who is asking, and on whose approval.

T-002. The agent used to tell the order system which customer it was acting
for, and the order system believed it. That is fine for a simulation of the
world and wrong for a store: a compromised agent could name any customer, and
after a person approved one refund it could add `refunds:write` to itself and
use it for another.

So the far end decides for itself, from two things the agent cannot invent:

- **A token addressed to it.** The agent exchanges the customer's session at
  the issuer for one with `aud` this system and `azp` the agent. It is verified
  here; an asserted `customer_id` is ignored.
- **The approval record, for anything the token does not already allow.** A
  refund needs a scope no customer session carries. The call names an approval,
  and this system loads it and checks it matches: granted, unexpired, the same
  customer, the same operation, the same idempotency key it was requested
  under, the same argument values, and not approved by the customer themself.

This is a separate package from `support_agent` on purpose, and the import
contract keeps it so: it is what a real store's tool server runs (T-017), and
the agent must not be able to see or shortcut its checks. It reuses only the
agent's token verifier and contracts, as a library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from support_agent.contracts import ApprovalRecords
from support_agent.contracts.failures import AgentFailure, Fault
from support_agent.identity import InvalidSession, Issuer, verify

SESSION_META = "aoas/session"
APPROVAL_META = "aoas/approval"
IDEMPOTENCY_META = "aoas/idempotency-key"
"""The binding's keys, declared on this side too: the far end does not import
the agent's transport to learn where its caller put things."""


class CallRefused(AgentFailure):
    """This system will not act on the call. One type, and a reason for the
    operator's log; the caller learns only that it was refused."""

    fault = Fault.REFUSED


def authoriser(
    *,
    issuer: Issuer,
    approvals: ApprovalRecords,
    required_scopes: Mapping[str, str],
    clock: Callable[[], int],
) -> Callable[[str, dict[str, Any], dict[str, object]], Any]:
    """The check a call passes before this system acts, as AgentTwin's
    `authorise` hook takes it.

    `required_scopes` is the binding's operation → scope table, the same one the
    agent's tool surface is filtered by. `clock` reads the time approvals are
    judged at; token expiry is the issuer's clock.
    """

    async def authorise(
        operation: str, arguments: dict[str, Any], meta: dict[str, object]
    ) -> dict[str, object]:
        session = meta.get(SESSION_META)
        token = session.get("token") if isinstance(session, dict) else None
        if not isinstance(token, str) or not token:
            raise CallRefused("a verified session is required")
        try:
            principal = verify(token, issuer=issuer)
        except InvalidSession as exc:
            raise CallRefused(f"session refused: {exc}") from None
        if not principal.customer_id:
            raise CallRefused("the session has no customer")

        needed = required_scopes.get(operation)
        if needed is not None and not principal.may(needed):
            await _approved(
                approvals, meta, operation, arguments, principal.customer_id, now=clock()
            )
        return {"customer_id": principal.customer_id, "acting_party": principal.party}

    return authorise


async def _approved(
    approvals: ApprovalRecords,
    meta: Mapping[str, object],
    operation: str,
    arguments: Mapping[str, Any],
    customer_id: str,
    *,
    now: int,
) -> None:
    """A scope the session lacks, supplied by an approval that matches this call.

    Argument **values** are compared, not names: the approval stores the
    arguments the request was made with and the tool may name them differently
    (F-013), but the order and the amount are the same values either way. Every
    value the call carries must be one the approval named.
    """
    approval_id = meta.get(APPROVAL_META)
    approval = await approvals.get(approval_id) if isinstance(approval_id, str) else None
    if approval is None:
        raise CallRefused(f"{operation} needs an approval, and none was named")
    reasons = [
        (not (approval.decided and approval.granted), "it was not granted"),
        (now >= approval.expires_at, "it has expired"),
        (approval.action != operation, f"it approved {approval.action}, not {operation}"),
        (approval.customer_id != customer_id, "it is for another customer"),
        (approval.decided_by in (None, customer_id), "nobody but the customer approved it"),
        (
            meta.get(IDEMPOTENCY_META) != approval.idempotency_key,
            "it was requested as another call",
        ),
        (
            not {_plain(v) for v in arguments.values()}
            <= {_plain(v) for v in approval.args.values()},
            "the call's arguments are not the ones approved",
        ),
    ]
    failed = [why for broken, why in reasons if broken]
    if failed:
        raise CallRefused(f"approval {approval.id} does not cover this call: {'; '.join(failed)}")


def _plain(value: object) -> str:
    return str(value)


__all__ = ["APPROVAL_META", "IDEMPOTENCY_META", "SESSION_META", "CallRefused", "authoriser"]
