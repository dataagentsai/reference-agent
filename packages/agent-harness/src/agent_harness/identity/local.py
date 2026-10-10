"""A local issuer: sessions signed in this process, for a developer's Mac and tests.

The `identity` port's `local-dev` adapter. A real RS256 key, made at start and
never written down, so the verifier the agent runs is the one production runs
(`identity.verify`) and only who signs differs. It can mint, which no other
adapter can: the agent verifies sessions and never signs them (T-002), so a
sign-in page exists only where this adapter is bound.

It is also the issuer's token exchange on a Mac (A1): `LocalExchange` turns a
verified session into a short-lived token for the far end's audience, and
`LocalWorkerLogin` is the payout workflow's own login there — what Entra's
on-behalf-of and client-credentials grants are on Azure.
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

from agent_harness.contracts import Identity
from agent_harness.identity import CLAIM_CUSTOMER, JWKS, InvalidSession, Issuer, verify

KID = "local-dev-key"


@dataclass
class LocalIssuer:
    url: str = "http://local-issuer.test/realms/agent"
    audience: str = "agent"
    ttl_s: int = 8 * 3600
    kid: str = KID
    """Which key this is. A process that makes a new key at every start gives
    it a new id (`adapters.identity`), so a far end holding the last start's
    key by id fetches the new one instead of refusing every token (A1)."""
    _key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
        repr=False,
    )

    @cached_property
    def jwks(self) -> dict[str, Any]:
        public = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

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
        party: str | None = None,
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
        if party:
            claims["azp"] = party
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})


@dataclass
class LocalExchange:
    """The local issuer's token exchange (`Exchange`): the caller's session,
    verified as the agent's, in; a token for `audience` out, issued to `party`,
    carrying the session's holder and the scopes both the session and the turn
    hold — never one the agent added to its identity in process. Short-lived,
    as an exchanged token is."""

    signer: LocalIssuer
    audience: str
    party: str = "agent"
    ttl_s: int = 300

    async def for_far_end(self, identity: Identity) -> str:
        if not identity.token:
            raise InvalidSession("no session to exchange")
        session = verify(identity.token, issuer=self.signer.issuer())
        return self.signer.mint(
            subject=session.subject,
            scopes=session.scopes & identity.scopes,
            customer_id=session.customer_id,
            ttl_s=self.ttl_s,
            audience=self.audience,
            party=self.party,
        )


@dataclass
class LocalWorkerLogin:
    """The payout workflow's own login (`Exchange`), when no person is there:
    a token for `audience` with no holder, issued to `party`, with the scopes
    the overlay gives it. The far end reads whose call it is from the approval
    the call names, never from this token or the identity it is handed."""

    signer: LocalIssuer
    audience: str
    scopes: frozenset[str]
    party: str = "agent-workflow"
    ttl_s: int = 300

    async def for_far_end(self, identity: Identity) -> str:
        del identity
        return self.signer.mint(
            subject=f"service-account-{self.party}",
            scopes=self.scopes,
            ttl_s=self.ttl_s,
            audience=self.audience,
            party=self.party,
        )


__all__ = ["KID", "LocalExchange", "LocalIssuer", "LocalWorkerLogin"]
