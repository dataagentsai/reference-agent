"""Entra ID's browser sign-in: the authorization code flow with PKCE and a nonce.

claims-fnol-azure A2. The OAuth 2.0 authorization code grant against the
tenant's v2.0 endpoints, as a confidential client: the browser is sent to
`/authorize` with a `state`, a PKCE challenge (S256) and an OpenID `nonce`; it
comes back with a code; the server redeems the code at `/token` with the PKCE
verifier and the app's client secret, and gets an access token addressed to the
app itself, an id token and a refresh token. Three checks tie the answer to the
request this browser made:

    state   the browser that comes back is the one that left (CSRF on the callback);
            the edge checks it against the sealed flow before anything is posted
    PKCE    the code is redeemed by whoever started the flow; Entra checks the verifier
    nonce   the id token was issued for this flow, not replayed from another

and two tie it to this app: the access token is checked by the identity
adapter's own verifier (`verify_entra`: signature, issuer, audience, expiry, the
holder claim, roles), and the id token, verified for the client id, must name
the same user (`oid`).

A confidential client because the code is redeemed by the server, where a
secret can be kept, and the app already has one (the on-behalf-of grant's, in
Key Vault): a public client would need a second registration or platform, and
its refresh tokens are shorter-lived. PKCE is sent anyway; Entra checks it for a
confidential client too.

`Sealer` carries the flow across the browser's round trip and names the
signed-in login afterwards: Fernet (AES with an HMAC and a timestamp), so a
cookie is neither readable nor forgeable by the browser and expires by itself.

Plain httpx through `entra.posted`, no MSAL, as the on-behalf-of grant is.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx2 as httpx
from cryptography.fernet import Fernet, InvalidToken

from agent_harness.identity import InvalidSession, Issuer, Principal, Verifier, decode
from agent_harness.identity.entra import AUTHORITY, EntraMalformed, posted, token_endpoint

OIDC_SCOPES = ("openid", "profile", "offline_access")
"""Always asked for: an id token (`openid`) naming the user's `oid` (`profile`),
and a refresh token (`offline_access`)."""


@dataclass(frozen=True)
class Flow:
    """One sign-in's secrets: made when the browser leaves, checked when it returns."""

    state: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def challenge(self) -> str:
        """The PKCE S256 challenge: base64url(SHA-256(verifier)), unpadded."""
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def sealed(self) -> dict[str, str]:
        return {"state": self.state, "verifier": self.verifier, "nonce": self.nonce}

    @classmethod
    def opened(cls, payload: dict[str, Any] | None) -> Flow | None:
        if not payload or not all(isinstance(payload.get(k), str) for k in cls().sealed()):
            return None
        return cls(state=payload["state"], verifier=payload["verifier"], nonce=payload["nonce"])


@dataclass(frozen=True)
class Tokens:
    """What the token endpoint answered: never printed."""

    access: str = field(repr=False)
    refresh: str | None = field(default=None, repr=False)
    id_token: str | None = field(default=None, repr=False)


class Sealer:
    """Small payloads sealed for the browser to carry and give back unchanged.

    A value this key did not seal, or sealed more than `ttl_s` ago, opens as
    `None`: one answer for tampered, expired and absent, so a caller has one
    branch and a prober learns nothing."""

    def __init__(self, key: bytes, *, clock: Callable[[], float] = time.time) -> None:
        self._fernet, self._clock = Fernet(key), clock

    def seal(self, payload: dict[str, str]) -> str:
        raw = json.dumps(payload).encode()
        return self._fernet.encrypt_at_time(raw, int(self._clock())).decode()

    def open(self, value: str | None, ttl_s: int) -> dict[str, Any] | None:
        if not value:
            return None
        try:
            raw = self._fernet.decrypt_at_time(value.encode(), ttl_s, int(self._clock()))
            payload = json.loads(raw)
        except (InvalidToken, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


class EntraLogin:
    """The tenant's sign-in, for one app registration and one redirect URI."""

    def __init__(
        self,
        tenant: str,
        *,
        issuer: Issuer,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: Iterable[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        base = f"{AUTHORITY}/{tenant}/oauth2/v2.0"
        self._authorize, self._logout, self._token = (
            f"{base}/authorize",
            f"{base}/logout",
            token_endpoint(tenant),
        )
        self._client_id, self._secret = client_id, client_secret
        self.redirect_uri = redirect_uri
        self._scope = " ".join(dict.fromkeys((*OIDC_SCOPES, *scopes)))
        self._sessions = issuer
        self._ids = replace(issuer, audience=client_id)  # an id token's `aud` is the client
        self._transport, self._timeout = transport, timeout_s

    def authorize_url(self, flow: Flow) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "response_mode": "query",
                "redirect_uri": self.redirect_uri,
                "scope": self._scope,
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": flow.challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._authorize}?{query}"

    def logout_url(self, post_logout_redirect_uri: str) -> str:
        """Where the browser goes to end its Entra session too (single sign-out)."""
        query = urllib.parse.urlencode({"post_logout_redirect_uri": post_logout_redirect_uri})
        return f"{self._logout}?{query}"

    async def redeem(self, code: str, flow: Flow) -> Tokens:
        """The code and this flow's verifier for tokens. A wrong verifier, a used
        or expired code: Entra's `invalid_grant`, raised as `InvalidSession`."""
        fields = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": flow.verifier,
            "scope": self._scope,
        }
        return _tokens(await self._post(fields, "sign-in"))

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        """`RefreshGrant`: a fresh access token, and the refresh token to keep
        (Entra rotates it; the old one is kept if none comes back)."""
        fields = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        tokens = _tokens(await self._post({**fields, "scope": self._scope}, "refresh"))
        return tokens.access, tokens.refresh or refresh_token

    def signed_in(self, tokens: Tokens, flow: Flow, verify: Verifier) -> Principal:
        """The verified login, or `InvalidSession`: the access token through the
        identity adapter's verifier, the id token's nonce this flow's, and both
        tokens about one user."""
        principal = verify(tokens.access)
        if not tokens.id_token:
            raise InvalidSession("the sign-in answered with no id token")
        claims = decode(tokens.id_token, issuer=self._ids, require=("nonce", "oid"))
        if not hmac.compare_digest(str(claims["nonce"]), flow.nonce):
            raise InvalidSession("the id token's nonce is not this sign-in's")
        access = decode(tokens.access, issuer=self._sessions, require=("oid",))
        if claims["oid"] != access["oid"]:
            raise InvalidSession("the id token and the access token name different users")
        return principal

    async def _post(self, fields: dict[str, str], grant: str) -> dict[str, Any]:
        form = {**fields, "client_id": self._client_id, "client_secret": self._secret}
        return await posted(self._token, form, self._transport, self._timeout, grant=grant)


def _tokens(body: dict[str, Any]) -> Tokens:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise EntraMalformed("the token endpoint answered 200 with no access_token")
    refresh, id_token = body.get("refresh_token"), body.get("id_token")
    return Tokens(
        access,
        refresh if isinstance(refresh, str) and refresh else None,
        id_token if isinstance(id_token, str) and id_token else None,
    )


__all__ = ["OIDC_SCOPES", "EntraLogin", "Flow", "Sealer", "Tokens"]
