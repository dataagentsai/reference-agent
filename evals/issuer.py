"""A local token issuer, for tests and the offline demo. Never for a deployment.

The agent can only verify (T-002): it holds an issuer's public keys and no
private one. Something still has to sign the sessions a test presents, and that
something must not live in `support_agent`, or the package would regain the
ability to forge a customer. So it lives here, beside the other harness pieces,
and signs RS256 exactly as Keycloak does, with the same claims.

One key pair per process. Generating RSA keys is the slow part, and nothing about
a test depends on a fresh one.
"""

from __future__ import annotations

import time
import uuid
from functools import cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from support_agent import identity as ident
from support_agent.contracts import Identity

URL = "http://local-issuer.test/realms/support"
AUDIENCE = "support-agent"
KID = "local-test-key"


@cache
def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks() -> dict[str, Any]:
    public = RSAAlgorithm.to_jwk(_private_key().public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]}


def issuer(audience: str = AUDIENCE) -> ident.Issuer:
    """What the agent is configured with to accept this issuer's sessions."""
    return ident.Issuer(url=URL, audience=audience, keys=ident.JWKS(jwks()))


def mint(
    customer_id: str | None,
    *,
    scopes: frozenset[str] = ident.CUSTOMER_SCOPES,
    subject: str | None = None,
    ttl_s: int = 900,
    now: int | None = None,
    audience: str = AUDIENCE,
    iss: str = URL,
    extra: dict[str, Any] | None = None,
) -> str:
    """A session as the realm issues one. `customer_id=None` is a staff login."""
    issued = now if now is not None else int(time.time())
    claims: dict[str, Any] = {
        "sub": subject or f"login-{customer_id or 'staff'}",
        "scp": sorted(scopes),
        "iss": iss,
        "aud": audience,
        "iat": issued,
        "exp": issued + ttl_s,
        "jti": uuid.uuid4().hex,
        **(extra or {}),
    }
    if customer_id is not None:
        claims[ident.CLAIM_CUSTOMER] = customer_id
    return jwt.encode(claims, _private_key(), algorithm="RS256", headers={"kid": KID})


class LocalExchange:
    """The issuer's token exchange, offline: what Keycloak's does, with the same
    claims. The customer's session in, a token for `audience` out, issued to the
    agent's client (`azp`), carrying the session's customer and its scopes and
    nothing the agent added to its identity in process."""

    def __init__(self, audience: str = "order-system", party: str = "support-agent") -> None:
        self.audience, self.party = audience, party
        self.exchanges = 0

    async def for_far_end(self, identity: Identity) -> str:
        if not identity.token:
            raise ident.InvalidSession("no session to exchange")
        claims = ident.verify(identity.token, issuer=issuer())
        self.exchanges += 1
        return mint(
            claims.customer_id,
            scopes=claims.scopes,
            subject=claims.subject,
            audience=self.audience,
            extra={"azp": self.party},
        )


class LocalRefresh:
    """The issuer's refresh grant, offline: logins held in memory, revocable.

    `login` is what the portal's callback receives; `revoke` is logout at the
    issuer. Every refresh is counted, so a test can see a burst of messages
    cost one.
    """

    def __init__(self) -> None:
        self._logins: dict[str, tuple[str | None, str]] = {}
        self.refreshes = 0

    def login(self, customer_id: str | None, *, subject: str | None = None) -> str:
        token = f"rt-{uuid.uuid4().hex}"
        self._logins[token] = (customer_id, subject or f"login-{customer_id or 'staff'}")
        return token

    def revoke(self, refresh_token: str) -> None:
        self._logins.pop(refresh_token, None)

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        self.refreshes += 1
        if refresh_token not in self._logins:
            raise ident.SessionEnded("refresh refused: 400")
        customer_id, subject = self._logins[refresh_token]
        scopes = ident.CUSTOMER_SCOPES if customer_id else ident.REVIEWER_SCOPES
        return mint(customer_id, subject=subject, scopes=scopes), refresh_token


__all__ = ["AUDIENCE", "URL", "LocalExchange", "LocalRefresh", "issuer", "jwks", "mint"]
