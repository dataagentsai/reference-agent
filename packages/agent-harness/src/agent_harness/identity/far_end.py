"""The far end's check on who is calling: the `authorise` port (claims-fnol-azure A1).

A tool server that owns rows decides for itself whose rows a call may touch. It
is handed the call — which operation, the scope that operation requires, the
bearer token the call carried, and the call's metadata — and answers with the
verified caller, or refuses. The agent's own word about whom it acts for is
never an input to a verifying adapter: only the token is.

    Asserted   believes the session the agent puts in `_meta`, as the AgentTwin
               world does. For a test binding only; the registry refuses it in
               any other overlay (`adapters.authorise`).
    Verified   checks a token for this far end's audience with the issuer's
               public keys (`identity.decode`), reads the holder from a named
               claim, and checks the operation's scope against what the token
               grants. The local and Entra adapters are this, each with its
               issuer's claim shapes.

A token with no holder claim is not refused here: it is a caller who owns no
rows, and the far end answers it as it answers a stranger. Whether such a caller
may act on a named approval (the payout workflow's own login) is the far end's
rule, read from `Caller.scopes`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_harness.contracts.failures import AgentFailure, Fault
from agent_harness.identity import CLAIM_CUSTOMER, InvalidSession, Issuer, decode

SESSION_META = "aoas/session"
"""Where the agent's MCP client puts what it says about the caller: the asserted
holder, and in process (no HTTP) the token. Declared here, as on every far end."""


class CallRefused(AgentFailure):
    """No verified caller, or one without the scope this operation requires.
    One type for every reason, so a caller cannot tell which half was wrong."""

    fault = Fault.REFUSED


@dataclass(frozen=True)
class Call:
    """What the far end knows about a call before it acts."""

    operation: str
    required_scope: str | None = None
    bearer: str | None = field(default=None, repr=False)
    """The token the call carried: the `Authorization` header over HTTP, or the
    session's `token` when the client is in the same process."""
    meta: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Caller:
    """A verified caller: whose rows it may touch, and what it may do."""

    holder: str | None
    """The policyholder (or customer) the token names; `None` owns no rows."""
    scopes: frozenset[str] = frozenset()
    party: str | None = None
    """`azp`: the client the token was issued to (the agent, or its workflow)."""
    subject: str = ""


class Authorise(Protocol):
    """The port: a call in, the verified caller out, or `CallRefused`."""

    async def __call__(self, call: Call) -> Caller: ...


def asserted_holder(meta: Mapping[str, object]) -> str | None:
    """The holder the agent asserts: `policyholder_id` (the AOAS session field)
    or `customer_id` (what the harness's client sends, F-5)."""
    session = meta.get(SESSION_META)
    if not isinstance(session, dict):
        return None
    holder = session.get("policyholder_id") or session.get("customer_id")
    return str(holder) if holder else None


class Asserted:
    """The AgentTwin world's belief: whoever the agent says, with any scope.
    Never a deployment's — the registry binds it in a test overlay only."""

    async def __call__(self, call: Call) -> Caller:
        granted = frozenset({call.required_scope} if call.required_scope else ())
        return Caller(holder=asserted_holder(call.meta), scopes=granted, subject="asserted")


Scopes = Callable[[dict[str, Any]], Iterable[str]]
"""How this issuer writes what a token grants: Keycloak and the local issuer a
list in `scp`; Entra a space-separated `scp` and app roles in `roles`."""


def listed_scopes(claims: dict[str, Any]) -> list[str]:
    """`scp` as a list, as Keycloak and the local issuer write it; anything else
    grants nothing (the rule `identity.verify` keeps)."""
    scp = claims.get("scp", [])
    return [s for s in scp if isinstance(s, str)] if isinstance(scp, list) else []


class Verified:
    """A token for this far end's audience, verified; the holder from a claim.

    `session_claim` is what this issuer names a token's id by (`jti`, Entra's
    `uti`), required like the rest of the registered claims. `holder_claim` is
    the claim the policyholder id is read from — only from there, never from
    `_meta`, never from `sub`."""

    def __init__(
        self,
        issuer: Issuer,
        *,
        session_claim: str = "jti",
        scopes: Scopes = listed_scopes,
        holder_claim: str = CLAIM_CUSTOMER,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._issuer, self._session = issuer, session_claim
        self._scopes, self._holder, self._clock = scopes, holder_claim, clock

    async def __call__(self, call: Call) -> Caller:
        if not call.bearer:
            raise CallRefused("a verified token is required")
        now = self._clock() if self._clock is not None else None
        try:
            claims = decode(call.bearer, issuer=self._issuer, require=(self._session,), now=now)
        except InvalidSession as exc:
            raise CallRefused(f"the token is not valid here: {exc}") from None
        granted = frozenset(str(s) for s in self._scopes(claims))
        if call.required_scope and call.required_scope not in granted:
            raise CallRefused(f"{call.operation} needs {call.required_scope}")
        holder = claims.get(self._holder)
        party = claims.get("azp")
        return Caller(
            holder=holder if isinstance(holder, str) and holder else None,
            scopes=granted,
            party=party if isinstance(party, str) else None,
            subject=str(claims["sub"]),
        )


__all__ = [
    "SESSION_META",
    "Asserted",
    "Authorise",
    "Call",
    "CallRefused",
    "Caller",
    "Scopes",
    "Verified",
    "asserted_holder",
    "listed_scopes",
]
