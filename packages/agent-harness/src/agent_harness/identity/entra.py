"""Entra ID as the issuer: its sessions verified, and exchanged on behalf of (T-099).

The Azure stack's `identity` binding (`entra-id`). Two pieces, each a
configuration of something this package already has rather than a second model
of identity:

- `entra_issuer` and `verify_entra`: the existing RS256 verifier, pointed at the
  tenant's v2.0 issuer and key set. Entra's access tokens differ from
  Keycloak's in two claims, and only those are read differently here: the
  session id is `uti`, not `jti`, and `scp` is a space-separated string (with
  `roles` for an application's own token, as a managed identity's is).
- `EntraOnBehalfOf`: the `Exchange` port as Entra realises it — the OAuth 2.0
  on-behalf-of grant, the customer's token in as the assertion and a token for
  the far end's scope out, issued to the agent's own app registration.

Plain HTTP through httpx, no MSAL: the grant is one form post, and a library
that also caches, retries and discovers would hide exactly the failure mapping
the caller branches on (AHC-0110).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx2 as httpx

from agent_harness.contracts import Identity
from agent_harness.contracts.failures import AgentFailure, Fault
from agent_harness.identity import (
    CLAIM_CUSTOMER,
    InvalidSession,
    Issuer,
    KeySource,
    Principal,
    RemoteJWKS,
    decode,
)

AUTHORITY = "https://login.microsoftonline.com"
OBO_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

REFUSALS = frozenset({"invalid_grant", "consent_required", "interaction_required"})
"""Entra reached and said no about *this session*: expired, revoked, or the
customer never consented. The customer's next login fixes it; a retry does not."""

MISCONFIGURATIONS = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request"}
)
"""Entra reached and said no about *this deployment*: a wrong secret, an app not
allowed the grant, a scope the far end does not expose. No request will work."""


class EntraUnreachable(AgentFailure):
    """The token endpoint timed out, dropped the connection, throttled (429) or
    failed (5xx). The only exchange failure worth waiting on."""

    fault = Fault.UNREACHABLE


class EntraMisconfigured(AgentFailure):
    """The app registration or the requested scope is wrong; fix the build."""

    fault = Fault.MISCONFIGURED


class EntraMalformed(AgentFailure):
    """A 200 with no token in it."""

    fault = Fault.MALFORMED


def token_endpoint(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/token"


def entra_issuer(tenant: str, audience: str, *, keys: KeySource | None = None) -> Issuer:
    """Whose sessions this service accepts, when Entra issues them.

    `tenant` is the directory's id (a GUID): a v2.0 token's `iss` names the id,
    never the domain, so a domain here refuses every session. `audience` is the
    app's client id or its `api://…` URI, whichever the app registration's
    `accessTokenAcceptedVersion` and identifier put in `aud`.
    """
    jwks = f"{AUTHORITY}/{tenant}/discovery/v2.0/keys"
    return Issuer(
        url=f"{AUTHORITY}/{tenant}/v2.0", audience=audience, keys=keys or RemoteJWKS(jwks)
    )


RoleScopes = Mapping[str, Iterable[str]]
"""An app role's value -> the scopes it grants, from the overlay (`role_scopes`).

Entra puts an app role a user is assigned in `roles` (its value, e.g.
`desk.handler`), and a role is coarser than a scope: the desk checks
`approvals:decide`, not a job title. The mapping says what each role may do;
the role itself is kept as a scope as well, so nothing that read it before
stops reading it."""


def verify_entra(
    token: str, *, issuer: Issuer, now: int | None = None, role_scopes: RoleScopes | None = None
) -> Principal:
    """`identity.verify` for Entra's claims: the same signature check, issuer,
    audience and expiry (`decode`), the session named by `uti`, and an app role
    granting the scopes `role_scopes` gives it."""
    claims = decode(token, issuer=issuer, now=now, require=("uti",))
    customer = claims.get(CLAIM_CUSTOMER)
    party = claims.get("azp")
    return Principal(
        subject=str(claims["sub"]),
        customer_id=customer if isinstance(customer, str) and customer else None,
        scopes=frozenset(entra_scopes(claims, role_scopes)),
        session=str(claims["uti"]),
        party=party if isinstance(party, str) else None,
        token=token,
    )


def entra_scopes(claims: dict[str, Any], role_scopes: RoleScopes | None = None) -> list[str]:
    """Delegated scopes from `scp`, a space-separated string, and application
    roles from `roles`, a list, each with the scopes `role_scopes` maps it to.
    Anything else grants nothing, as in `verify`."""
    scp = claims.get("scp", "")
    roles = claims.get("roles", [])
    delegated = scp.split() if isinstance(scp, str) else []
    granted = [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []
    mapped = [str(s) for r in granted for s in (role_scopes or {}).get(r, ())]
    return delegated + granted + mapped


class EntraOnBehalfOf:
    """The on-behalf-of grant at the tenant's token endpoint (`Exchange`).

    The customer's session — addressed to the agent — goes in as `assertion`;
    out comes a token for `scope` (the far end's `api://…/.default` or a named
    scope), issued to the agent's app, carrying the customer as its subject.

    The agent proves itself with a client secret, or with a client assertion: a
    callable returning a signed JWT, which on Azure is the managed identity's
    token for `api://AzureADTokenExchange` behind a federated credential, so no
    secret exists at all. Exactly one of the two.

    Tokens are cached per session until thirty seconds before they expire, as
    `TokenExchange` does, so a turn of eight tool calls is one exchange.
    """

    def __init__(
        self,
        tenant: str,
        *,
        client_id: str,
        scope: str,
        client_secret: str | None = None,
        client_assertion: Callable[[], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (client_secret is None) == (client_assertion is None):
            raise EntraMisconfigured("give exactly one of client_secret and client_assertion")
        self._endpoint = token_endpoint(tenant)
        self._client_id, self._scope = client_id, scope
        self._secret, self._assertion = client_secret, client_assertion
        self._transport, self._timeout, self._clock = transport, timeout_s, clock
        self._cache: dict[str, tuple[str, float]] = {}

    async def for_far_end(self, identity: Identity) -> str:
        if not identity.token:
            raise InvalidSession("no session to exchange")
        now = float(self._clock())
        cached = self._cache.get(identity.token)
        if cached is not None and cached[1] - 30 > now:
            return cached[0]
        body = await self._post(identity.token)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise EntraMalformed("on-behalf-of answered 200 with no access_token")
        self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
        self._cache[identity.token] = (token, now + float(body.get("expires_in", 60)))
        return token

    def form(self, assertion: str) -> dict[str, str]:
        """What is posted: public so a test can read it without a server."""
        fields = {
            "grant_type": OBO_GRANT,
            "requested_token_use": "on_behalf_of",
            "assertion": assertion,
            "scope": self._scope,
            "client_id": self._client_id,
        }
        if self._assertion is not None:
            fields["client_assertion_type"] = ASSERTION_TYPE
            fields["client_assertion"] = self._assertion()
        else:
            fields["client_secret"] = str(self._secret)
        return fields

    async def _post(self, assertion: str) -> dict[str, Any]:
        return await _posted(self._endpoint, self.form(assertion), self._transport, self._timeout)


class EntraClientCredentials:
    """The payout workflow's own login on Azure (`Exchange`, A1): the
    client-credentials grant for the far end's `scope` (its `/.default`), as
    the agent's app. No person is there when a handler approves an hour later,
    so there is no session to exchange; the token carries the app roles the far
    end granted the agent (`roles`, e.g. `payouts.issue`) and no holder — the
    far end reads whose call it is from the approval the call names.

    Cached until thirty seconds before it expires, one token for every call."""

    def __init__(
        self,
        tenant: str,
        *,
        client_id: str,
        scope: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoint = token_endpoint(tenant)
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
        self._transport, self._timeout, self._clock = transport, timeout_s, clock
        self._held: tuple[str, float] | None = None

    async def for_far_end(self, identity: Identity) -> str:
        del identity
        now = float(self._clock())
        if self._held is not None and self._held[1] - 30 > now:
            return self._held[0]
        body = await _posted(self._endpoint, self._form, self._transport, self._timeout)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise EntraMalformed("client credentials answered 200 with no access_token")
        self._held = (token, now + float(body.get("expires_in", 60)))
        return token


async def _posted(
    endpoint: str,
    form: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
            response = await http.post(endpoint, data=form)
    except httpx.TransportError as exc:  # timeouts are transport errors too
        raise EntraUnreachable(f"token endpoint: {type(exc).__name__}") from exc
    return _answer(response)


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The body, or the failure kind the status and Entra's `error` say it is.
    Entra's description goes to the operator's span, never to the caller."""
    status = response.status_code
    if status == 429 or status >= 500:
        raise EntraUnreachable(f"token endpoint answered {status}")
    try:
        body = response.json()
    except ValueError as exc:
        raise EntraMalformed(f"token endpoint answered {status} with no JSON") from exc
    if not isinstance(body, dict):
        raise EntraMalformed(f"token endpoint answered {status} with a non-object")
    if status == 200:
        return body
    error = str(body.get("error", ""))
    if error in REFUSALS:
        raise InvalidSession(f"on-behalf-of refused: {error}")
    if error in MISCONFIGURATIONS:
        raise EntraMisconfigured(f"on-behalf-of refused: {error}")
    raise InvalidSession(f"on-behalf-of refused: {status} {error or 'no error code'}")


__all__ = [
    "EntraClientCredentials",
    "EntraMalformed",
    "EntraMisconfigured",
    "EntraOnBehalfOf",
    "EntraUnreachable",
    "RoleScopes",
    "entra_issuer",
    "entra_scopes",
    "token_endpoint",
    "verify_entra",
]
