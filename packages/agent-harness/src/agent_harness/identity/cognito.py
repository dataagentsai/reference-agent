"""Amazon Cognito's sessions, and the agent's own issuer for the far end (T-123).

Cognito writes its access tokens in its own shape, so they get their own check,
as Entra's do (`identity.entra`):

    no `aud`         an access token names the app client in `client_id`; the
                     check is that it is one of ours, and `token_use` is
                     `access` (an ID token is for the browser, never an API)
    `scope`          a space-separated string, like Entra's `scp`
    `cognito:groups` a list; each group grants the scopes the overlay's
                     `role_scopes` maps it to, as Entra's app roles do
    the holder       not a Cognito claim at all: a pre-token-generation trigger
                     (version 2, which can write to access tokens) adds it, and
                     `holder_claim` names it

Cognito has no on-behalf-of exchange (no RFC 8693 token exchange). So the far
end's token comes from the agent's own issuer: `AgentIssuer` signs short-lived
tokens for the far end's audience with one RSA key held in Secrets Manager, so
every task signs with the same key and the far end can check any of them against
the keys the app publishes (`jwks`). The far end binds `authorise: agent-issuer`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_harness.contracts import Identity
from agent_harness.identity import (
    ALGORITHMS,
    CLAIM_CUSTOMER,
    InvalidSession,
    Issuer,
    KeySource,
    Principal,
    RemoteJWKS,
    holder_of,
)
from agent_harness.identity.local import LocalIssuer

RoleScopes = Mapping[str, Iterable[str]]


def cognito_url(region: str, user_pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def cognito_issuer(
    region: str, user_pool_id: str, client_id: str, *, keys: KeySource | None = None
) -> Issuer:
    """Whose sessions this service accepts: one user pool, one app client."""
    url = cognito_url(region, user_pool_id)
    return Issuer(
        url=url, audience=client_id, keys=keys or RemoteJWKS(f"{url}/.well-known/jwks.json")
    )


def verify_cognito(
    token: str,
    *,
    issuer: Issuer,
    role_scopes: RoleScopes | None = None,
    holder_claim: str = CLAIM_CUSTOMER,
    now: int | None = None,
) -> Principal:
    """The same signature, issuer and expiry checks as `identity.decode`, with
    the app client in `client_id` standing in for `aud`."""
    try:
        if jwt.get_unverified_header(token).get("alg") not in ALGORITHMS:
            raise InvalidSession("algorithm not accepted")
        claims: dict[str, Any] = jwt.decode(
            token,
            issuer.keys.key_for(token),
            algorithms=list(ALGORITHMS),
            issuer=issuer.url,
            options={
                "require": ["exp", "iat", "sub", "iss", "jti", "token_use", "client_id"],
                "verify_aud": False,  # Cognito access tokens carry none; client_id below
                "verify_exp": now is None,
                "verify_iat": False,
            },
            leeway=0,
        )
        if now is not None and claims["exp"] <= now:
            raise jwt.ExpiredSignatureError("expired")
    except jwt.PyJWTError as exc:
        raise InvalidSession(str(exc)) from exc
    if claims["token_use"] != "access":
        raise InvalidSession("not an access token")
    if claims["client_id"] != issuer.audience:
        raise InvalidSession("issued to another app client")
    return Principal(
        subject=str(claims["sub"]),
        customer_id=holder_of(claims.get(holder_claim)),
        scopes=frozenset(cognito_scopes(claims, role_scopes)),
        session=str(claims["jti"]),
        party=str(claims["client_id"]),
        token=token,
    )


def cognito_scopes(claims: Mapping[str, Any], role_scopes: RoleScopes | None = None) -> list[str]:
    """`scope` split on spaces, the groups themselves, and what each group grants.
    Any other shape grants nothing."""
    scope = claims.get("scope", "")
    groups = claims.get("cognito:groups", [])
    granted = scope.split() if isinstance(scope, str) else []
    named = [g for g in groups if isinstance(g, str)] if isinstance(groups, list) else []
    mapped = [str(s) for g in named for s in (role_scopes or {}).get(g, ())]
    return granted + named + mapped


def agent_issuer(url: str, private_key_pem: str, audience: str = "agent") -> LocalIssuer:
    """The agent's own issuer for far-end tokens, from a PEM key every task shares.

    The key id is the key's RFC 7638 thumbprint: the same key gives the same id
    on every task and every restart, and a rotated key a new one, so a far end
    caching keys by id fetches the new set instead of refusing."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidSession("the agent issuer's key must be RSA (RS256)")
    public = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    canonical = json.dumps({k: public[k] for k in ("e", "kty", "n")}, separators=(",", ":"))
    kid = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()
    )
    return LocalIssuer(url=url, audience=audience, kid=kid, _key=key)


@dataclass
class CognitoExchange:
    """A Cognito session in; a token for the far end out, signed by the agent's
    issuer (`Exchange`). It carries the session's holder and only the scopes both
    the session and the turn hold, as `LocalExchange` does."""

    verify: Any
    """This adapter's verifier: the session is checked again, never trusted."""
    signer: LocalIssuer
    audience: str
    party: str = "agent"
    ttl_s: int = 300

    async def for_far_end(self, identity: Identity) -> str:
        if not identity.token:
            raise InvalidSession("no session to exchange")
        session: Principal = self.verify(identity.token)
        return self.signer.mint(
            subject=session.subject,
            scopes=session.scopes & identity.scopes,
            customer_id=session.customer_id,
            ttl_s=self.ttl_s,
            audience=self.audience,
            party=self.party,
        )


__all__ = [
    "CognitoExchange",
    "agent_issuer",
    "cognito_issuer",
    "cognito_scopes",
    "cognito_url",
    "verify_cognito",
]
