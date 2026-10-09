"""A local issuer: sessions signed in this process, for a developer's Mac and tests.

The `identity` port's `local-dev` adapter. A real RS256 key, made at start and
never written down, so the verifier the agent runs is the one production runs
(`identity.verify`) and only who signs differs. It can mint, which no other
adapter can: the agent verifies sessions and never signs them (T-002), so a
sign-in page exists only where this adapter is bound.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_harness.identity import CLAIM_CUSTOMER, JWKS, Issuer

KID = "local-dev-key"


@dataclass
class LocalIssuer:
    url: str = "http://local-issuer.test/realms/agent"
    audience: str = "agent"
    ttl_s: int = 8 * 3600
    _key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        repr=False,
    )

    @cached_property
    def jwks(self) -> dict[str, Any]:
        public = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]}

    def issuer(self) -> Issuer:
        return Issuer(url=self.url, audience=self.audience, keys=JWKS(self.jwks))

    def mint(
        self,
        *,
        subject: str,
        scopes: frozenset[str] | set[str],
        customer_id: str | None = None,
        name: str = "",
        ttl_s: int | None = None,
        audience: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "scp": sorted(scopes),
            "iss": self.url,
            "aud": audience or self.audience,
            "iat": now,
            "exp": now + (self.ttl_s if ttl_s is None else ttl_s),
            "jti": uuid.uuid4().hex,
        }
        if name:
            claims["name"] = name
        if customer_id:
            claims[CLAIM_CUSTOMER] = customer_id
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": KID})


__all__ = ["KID", "LocalIssuer"]
