"""Sessions that cross the network: exchanged for the far end, or resumed for a channel.

Split from `identity` itself, which verifies and holds the shapes, so that the
part a test can run with no issuer at all stays small. Everything here talks to
the issuer's token endpoint.

- `TokenExchange` (T-002): the customer's session in, a token addressed to the
  order system out, issued to the agent's own client.
- `KeycloakRefresh` and `Resume` (T-026): a login the portal kept server-side,
  turned back into a live customer session when a chat channel's message
  arrives, and refused once the customer has logged out.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from support_agent.contracts import Identity, SessionStore, StoredSession
from support_agent.identity import (
    InvalidSession,
    Issuer,
    Principal,
    RefreshGrant,
    SessionEnded,
    discovery,
    verify,
)


class TokenExchange:
    """RFC 8693 at the issuer: the customer's session in, a token for the order
    system out, issued to the agent's own client.

    The far end then verifies a token addressed to it (`aud`), naming the agent
    as the party that asked (`azp`) and the customer as whose authority it
    carries. The customer's own session is addressed to the agent and would be
    refused there, which is the point: a token is good for one audience.

    Exchanged tokens are cached until shortly before they expire, keyed by the
    session they came from, so a turn of eight tool calls is one exchange.
    """

    def __init__(
        self,
        token_endpoint: str,
        *,
        client_id: str,
        client_secret: str,
        audience: str,
        scope: str,
        clock: Any = time.time,
    ) -> None:
        self._endpoint = token_endpoint
        self._client = (client_id, client_secret)
        self._audience, self._scope, self._clock = audience, scope, clock
        self._cache: dict[str, tuple[str, float]] = {}

    @classmethod
    def discover(cls, issuer_url: str, **kwargs: Any) -> TokenExchange:
        return cls(str(discovery(issuer_url)["token_endpoint"]), **kwargs)

    async def for_far_end(self, identity: Identity) -> str:
        if not identity.token:
            raise InvalidSession("no session to exchange")
        now = float(self._clock())
        cached = self._cache.get(identity.token)
        if cached is not None and cached[1] - 30 > now:
            return cached[0]
        body = await asyncio.to_thread(self._post, identity.token)
        token = str(body["access_token"])
        self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
        self._cache[identity.token] = (token, now + float(body.get("expires_in", 60)))
        return token

    def _post(self, subject_token: str) -> dict[str, Any]:
        client_id, secret = self._client
        form = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": subject_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "audience": self._audience,
                "scope": self._scope,
                "client_id": client_id,
                "client_secret": secret,
            }
        ).encode()
        try:
            request = urllib.request.Request(self._endpoint, data=form)
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                body: dict[str, Any] = json.load(response)
                return body
        except urllib.error.HTTPError as exc:
            # The issuer's reason is for the operator's span, not the caller.
            raise InvalidSession(f"exchange refused: {exc.code}") from exc


class KeycloakRefresh:
    """The OAuth refresh grant at the realm, as the portal's own client."""

    def __init__(self, token_endpoint: str, *, client_id: str, client_secret: str) -> None:
        self._endpoint = token_endpoint
        self._client = (client_id, client_secret)

    @classmethod
    def discover(cls, issuer_url: str, **kwargs: Any) -> KeycloakRefresh:
        return cls(str(discovery(issuer_url)["token_endpoint"]), **kwargs)

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        body = await asyncio.to_thread(self._post, refresh_token)
        return str(body["access_token"]), str(body.get("refresh_token") or refresh_token)

    def _post(self, refresh_token: str) -> dict[str, Any]:
        client_id, secret = self._client
        form = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": secret,
            }
        ).encode()
        try:
            request = urllib.request.Request(self._endpoint, data=form)
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                body: dict[str, Any] = json.load(response)
                return body
        except urllib.error.HTTPError as exc:
            # 400 invalid_grant is the issuer saying the login is over.
            raise SessionEnded(f"refresh refused: {exc.code}") from exc


class Resume:
    """A stored login to a fresh customer identity, for a channel acting on it.

    One refresh per login at a time, and the verified session reused for half a
    minute: a customer who sends three messages in a second is one refresh, not
    three racing each other at the issuer. Short on purpose, so a logout reaches
    the agent within that half minute even without `forget`.
    """

    def __init__(
        self,
        store: SessionStore,
        grant: RefreshGrant,
        *,
        issuer: Issuer,
        clock: Any = time.time,
    ) -> None:
        self._store, self._grant, self._issuer, self._clock = store, grant, issuer, clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._fresh: dict[str, tuple[Principal, float]] = {}

    async def identity_for(self, subject: str) -> Identity:
        async with self._locks.setdefault(subject, asyncio.Lock()):
            now = float(self._clock())
            cached = self._fresh.get(subject)
            if cached is not None and cached[1] - 30 > now:
                return cached[0].as_customer()
            principal = await self._refreshed(subject, now)
            self._fresh[subject] = (principal, now + 60)
            return principal.as_customer()

    async def _refreshed(self, subject: str, now: float) -> Principal:
        stored = await self._store.get(subject)
        if stored is None:
            raise SessionEnded(f"no stored login for {subject!r}")
        try:
            access, keep = await self._grant.refresh(stored.refresh_token)
        except SessionEnded:
            await self._store.delete(subject)
            self._fresh.pop(subject, None)
            raise
        principal = verify(access, issuer=self._issuer)
        if principal.subject != subject:
            # A stored token that belongs to another login is not this one's,
            # whatever the row is keyed by.
            await self._store.delete(subject)
            raise SessionEnded(f"the stored login for {subject!r} is someone else's")
        await self._store.put(
            StoredSession(subject=subject, refresh_token=keep, updated_at=int(now))
        )
        return principal

    def forget(self, subject: str) -> None:
        """Drop the cached token, as logout does, so the next message refreshes
        and finds the session gone."""
        self._fresh.pop(subject, None)


class KeycloakLogin:
    """The realm's authorization code flow with PKCE, for the portal's client."""

    def __init__(self, document: dict[str, Any], *, client_id: str, client_secret: str) -> None:
        self._authorize = str(document["authorization_endpoint"])
        self._token = str(document["token_endpoint"])
        self._logout = str(document["end_session_endpoint"])
        self._client = (client_id, client_secret)

    @classmethod
    def discover(cls, issuer_url: str, **kwargs: Any) -> KeycloakLogin:
        return cls(discovery(issuer_url), **kwargs)

    def authorize_url(self, *, redirect_uri: str, state: str, challenge: str) -> str:
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._client[0],
                "redirect_uri": redirect_uri,
                "scope": "openid",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._authorize}?{query}"

    async def redeem(self, code: str, *, redirect_uri: str, verifier: str) -> tuple[str, str]:
        body = await asyncio.to_thread(
            self._post,
            self._token,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        return str(body["access_token"]), str(body["refresh_token"])

    async def end(self, refresh_token: str) -> None:
        await asyncio.to_thread(self._post, self._logout, {"refresh_token": refresh_token})

    def _post(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        client_id, secret = self._client
        form = urllib.parse.urlencode(
            {**fields, "client_id": client_id, "client_secret": secret}
        ).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=form), timeout=10) as r:  # noqa: S310
                raw = r.read()
        except urllib.error.HTTPError as exc:
            raise SessionEnded(f"the realm refused: {exc.code}") from exc
        parsed: dict[str, Any] = json.loads(raw) if raw else {}
        return parsed


__all__ = ["KeycloakLogin", "KeycloakRefresh", "Resume", "TokenExchange"]
