"""Who the system is acting as.

L16 · P1 and P5. A session token is issued by the identity provider, verified at
the edge, and carried into every tool call, where the tool server authorises
against it.

The rule that matters: **authorisation is decided from the token, never from
anything the model said.** AAC-0106 — the system prompt is not a security
boundary — and AAC-0057 — least privilege enforced server-side. A model that has
been talked into believing the customer is someone else still cannot act as them,
because the claim it would need never leaves this module.

**This module verifies and cannot sign (T-002, 2026-09-16).** Until then a token
was HS256: one shared secret both signed and verified, so anything able to check
a session could forge one, and the agent held that secret. Now the issuer signs
with a private key it never shares and the agent verifies with the public half,
fetched from the issuer's JWKS. A compromised agent can read sessions and cannot
write them. There is no `mint` here any more; tests sign with a local issuer in
`evals/issuer.py`, outside the package.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from pydantic import BaseModel, ConfigDict, Field

from support_agent.contracts import Identity
from support_agent.contracts.failures import AgentFailure, Fault

ALGORITHMS = ("RS256",)
"""The only algorithm accepted. An allow-list, not a preference: a token whose
header says HS256 or `none` is refused before any key is consulted, which is
what closes algorithm confusion (a public key reused as an HMAC secret)."""

CLAIM_CUSTOMER = "customer_id"
"""Whose orders a session may touch, as its own claim (T-002 decision 1).

Not `sub`. `sub` is the login; the customer is a row in the store, linked to the
login by an administrator. Keeping them apart means a username change, a second
identity provider or a staff login with no customer at all changes nothing
here."""

# Scopes are coarse on purpose. A scope per tool becomes a scope nobody audits.
SCOPE_ORDERS_READ = "orders:read"
SCOPE_ORDERS_WRITE = "orders:write"
SCOPE_RETURNS_WRITE = "returns:write"
SCOPE_REFUNDS_WRITE = "refunds:write"
SCOPE_ESCALATIONS_READ = "escalations:read"
SCOPE_ESCALATIONS_REVIEW = "escalations:review"

CUSTOMER_SCOPES = frozenset({SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE, SCOPE_RETURNS_WRITE})
"""What a customer session holds. The issuer grants it, as the `customer` role;
this constant is what the realm is checked against, not where it comes from.

`refunds:write` is deliberately absent. A refund is irreversible and above the
approval threshold it needs a human, so the agent acting as the customer must
not hold the scope that would let it skip that gate. The gate is not the only
control; it is the second one.
"""

REVIEWER_SCOPES = frozenset({SCOPE_ESCALATIONS_READ, SCOPE_ESCALATIONS_REVIEW})
"""What a person working the escalation desk holds, as the `reviewer` role.

Disjoint from `CUSTOMER_SCOPES`, and that is the whole design. A reviewer holds
no `orders:*` at all: the desk reads and closes escalations, and if it needs to
act on an order it does so as itself through the ordinary surface, where the
same authorisation applies to it as to anyone. And a customer session can never
hold `escalations:review`, which is what makes `resolve`'s refusal to let a
customer close their own case a control rather than a convention — the outcome
label is the input to the over-escalation rate, so the party being measured must
not be able to write it.
"""


class InvalidSession(AgentFailure):
    """The token is absent, expired, tampered with, or issued by someone else.

    One exception for all of these on purpose: distinguishing them for the caller
    tells an attacker which half of the guess was right.
    """

    fault = Fault.REFUSED


class NotACustomer(AgentFailure):
    """A valid session with no customer behind it, such as a reviewer's, used
    where only a customer may act. Refused, never defaulted to `sub`: a fallback
    would make a staff login a customer whose id happens to be a UUID."""

    fault = Fault.REFUSED


class KeySource(Protocol):
    """Where the issuer's public keys come from."""

    def key_for(self, token: str) -> Any: ...


class JWKS:
    """A key set held in memory: a pinned deployment, or a test issuer's."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._keys = jwt.PyJWKSet.from_dict(document)

    def key_for(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._keys.keys:
            if key.key_id == kid:
                return key.key
        raise InvalidSession(f"no key with id {kid!r}")


class RemoteJWKS:
    """The issuer's published key set, fetched and cached.

    PyJWT's client refetches when a token names a key it has not seen, which is
    how a key rotation at the issuer reaches the agent without a restart.
    """

    def __init__(self, jwks_uri: str, *, lifespan_s: int = 300) -> None:
        self._client = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=lifespan_s)

    @classmethod
    def discover(cls, issuer_url: str) -> RemoteJWKS:
        """From the issuer's OpenID discovery document, at startup. An issuer that
        cannot be reached fails the process, not the first customer."""
        return cls(str(discovery(issuer_url)["jwks_uri"]))

    def key_for(self, token: str) -> Any:
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise InvalidSession(str(exc)) from exc


def discovery(issuer_url: str) -> dict[str, Any]:
    """The issuer's OpenID discovery document."""
    url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 — configured URL
        document: dict[str, Any] = json.load(response)
        return document


@dataclass(frozen=True)
class Issuer:
    """Whose sessions this service accepts: the issuer's name, this service's
    audience, and the issuer's public keys.

    `audience` matters the moment a second service shares the issuer. Without
    it, a token minted for the store's API would be accepted by this agent.
    """

    url: str
    audience: str
    keys: KeySource


class Principal(BaseModel):
    """A verified session: who logged in, and what they may do.

    Wider than `Identity`, which is always a customer acting through the agent.
    A reviewer is a principal and never an identity.
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    """`sub`: the login, for the record. Never used to find an order."""
    customer_id: str | None
    scopes: frozenset[str] = Field(default_factory=frozenset)
    session: str
    """`jti`: which session, so the record can say which login did a thing and
    not only which run."""
    party: str | None = None
    """`azp`: the client the session was issued to. After a token exchange it is
    the agent, so the far end's record can say *the support agent, acting for
    C-1042* rather than only *C-1042*."""
    token: str = Field(repr=False)

    def may(self, scope: str) -> bool:
        return scope in self.scopes

    def as_customer(self) -> Identity:
        if not self.customer_id:
            raise NotACustomer(f"session {self.session!r} has no customer")
        return Identity(
            customer_id=self.customer_id,
            scopes=self.scopes,
            token=self.token,
            subject=self.subject,
            session=self.session,
        )


def verify(token: str, *, issuer: Issuer, now: int | None = None) -> Principal:
    """Decode into a `Principal`, or refuse.

    Note what is *not* here: no fallback to an unverified decode, no "trust the
    subject if the signature is missing", no algorithm the header may choose. A
    token that does not verify produces no principal at all, so there is no
    partially-trusted path for a caller to take by accident.
    """
    try:
        if jwt.get_unverified_header(token).get("alg") not in ALGORITHMS:
            raise InvalidSession("algorithm not accepted")
        claims = jwt.decode(
            token,
            issuer.keys.key_for(token),
            algorithms=list(ALGORITHMS),
            issuer=issuer.url,
            audience=issuer.audience,
            options={
                "require": ["exp", "iat", "sub", "iss", "aud", "jti"],
                # With an injected clock we check expiry ourselves below, so the
                # library must not compare against the wall clock as well.
                "verify_exp": now is None,
                "verify_iat": False,
            },
            leeway=0,
        )
        if now is not None and claims["exp"] <= now:
            raise jwt.ExpiredSignatureError("expired")
    except jwt.PyJWTError as exc:
        raise InvalidSession(str(exc)) from exc

    customer = claims.get(CLAIM_CUSTOMER)
    return Principal(
        subject=str(claims["sub"]),
        customer_id=customer if isinstance(customer, str) and customer else None,
        scopes=frozenset(_scopes(claims)),
        session=str(claims["jti"]),
        party=claims.get("azp") if isinstance(claims.get("azp"), str) else None,
        token=token,
    )


def _scopes(claims: dict[str, Any]) -> list[str]:
    """`scp` as a list. Anything else grants nothing, rather than being coerced:
    a string `scp` iterated character by character is a scope list of letters."""
    scp = claims.get("scp", [])
    return [s for s in scp if isinstance(s, str)] if isinstance(scp, list) else []


class Exchange(Protocol):
    """Turns a customer's session into one addressed to the far end (T-002)."""

    async def for_far_end(self, identity: Identity) -> str: ...


class SessionEnded(AgentFailure):
    """No live login to act for: never stored, logged out, or ended at the issuer.

    The channel cannot fix it and neither can a retry. The customer logs in again.
    """

    fault = Fault.REFUSED


class RefreshGrant(Protocol):
    """A refresh token in, a fresh access token and the refresh token to keep out."""

    async def refresh(self, refresh_token: str) -> tuple[str, str]: ...


class Login(Protocol):
    """The issuer's login, as a portal drives it: send the browser to authorize,
    redeem the code it comes back with, and end the session at logout (T-026)."""

    def authorize_url(self, *, redirect_uri: str, state: str, challenge: str) -> str: ...

    async def redeem(self, code: str, *, redirect_uri: str, verifier: str) -> tuple[str, str]:
        """The access token and the refresh token to keep."""
        ...

    async def end(self, refresh_token: str) -> None: ...


def require(identity: Identity | Principal, scope: str) -> None:
    """Assert a scope before an action is attempted.

    Called at the tool boundary, P5 — the last place an action can be stopped
    while stopping it is still cheap.
    """
    if not identity.may(scope):
        who = identity.customer_id if isinstance(identity, Identity) else identity.subject
        raise InvalidSession(f"identity {who!r} lacks scope {scope!r}")


__all__ = [
    "ALGORITHMS",
    "CLAIM_CUSTOMER",
    "CUSTOMER_SCOPES",
    "Exchange",
    "JWKS",
    "REVIEWER_SCOPES",
    "SCOPE_ESCALATIONS_READ",
    "SCOPE_ESCALATIONS_REVIEW",
    "SCOPE_ORDERS_READ",
    "SCOPE_ORDERS_WRITE",
    "SCOPE_REFUNDS_WRITE",
    "SCOPE_RETURNS_WRITE",
    "InvalidSession",
    "Login",
    "Issuer",
    "KeySource",
    "NotACustomer",
    "Principal",
    "RefreshGrant",
    "SessionEnded",
    "RemoteJWKS",
    "discovery",
    "require",
    "verify",
]
