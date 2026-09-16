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


__all__ = ["AUDIENCE", "URL", "issuer", "jwks", "mint"]
