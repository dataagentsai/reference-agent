"""The `identity` port: whose sessions the agent accepts, and the token exchange.

    local-dev   sessions signed in this process (`identity.local`); the only
                adapter that can mint, so a sign-in page exists only here
    keycloak    the Open Stack's realm: its published keys, RFC 8693 exchange
    entra-id    Entra's v2.0 issuer for the tenant, Entra's claim shapes, and
                the on-behalf-of grant

The product is `Sessions`: the issuer, the verifier that reads this issuer's
claim shapes (Entra's `scp` is a string, Keycloak's a list), the exchange when
one is configured, and the signer where there is one. The hook `keys` replaces
a remote key set with a local one, which is how the contract tests run with no
network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from agent_harness import identity as ident
from agent_harness.adapters import Adapter, Setting, Wiring
from agent_harness.identity.local import LocalIssuer


@dataclass(frozen=True)
class Sessions:
    issuer: ident.Issuer
    verify: Callable[[str], ident.Principal]
    exchange: ident.Exchange | None = None
    signer: LocalIssuer | None = None


@asynccontextmanager
async def _local(wiring: Wiring) -> AsyncIterator[Sessions]:
    signer = LocalIssuer(url=str(wiring.settings["url"]), audience=str(wiring.settings["audience"]))
    issuer = signer.issuer()
    yield Sessions(issuer, lambda token: ident.verify(token, issuer=issuer), None, signer)


@asynccontextmanager
async def _keycloak(wiring: Wiring) -> AsyncIterator[Sessions]:
    from agent_harness.identity.sessions import TokenExchange

    settings = wiring.settings
    url = str(settings["issuer_url"])
    keys: Any = wiring.hooks.get("keys") or ident.RemoteJWKS.discover(url)
    issuer = ident.Issuer(url=url, audience=str(settings["audience"]), keys=keys)
    exchange = None
    if settings.get("client_id"):
        exchange = TokenExchange.discover(
            url,
            client_id=str(settings["client_id"]),
            client_secret=str(settings["client_secret"]),
            audience=str(settings["far_end_audience"]),
            scope=str(settings.get("scope") or ""),
        )
    yield Sessions(issuer, lambda token: ident.verify(token, issuer=issuer), exchange)


@asynccontextmanager
async def _entra(wiring: Wiring) -> AsyncIterator[Sessions]:
    from agent_harness.identity import entra

    settings = wiring.settings
    tenant = str(settings["tenant"])
    issuer = entra.entra_issuer(tenant, str(settings["audience"]), keys=wiring.hooks.get("keys"))
    exchange = None
    if settings.get("client_id") and settings.get("client_secret"):
        exchange = entra.EntraOnBehalfOf(
            tenant,
            client_id=str(settings["client_id"]),
            scope=str(settings["far_end_scope"]),
            client_secret=str(settings["client_secret"]),
        )
    yield Sessions(issuer, lambda token: entra.verify_entra(token, issuer=issuer), exchange)


LOCAL_DEV = Adapter(
    "identity",
    "local-dev",
    _local,
    {
        "url": Setting(default="http://local-issuer.test/realms/agent"),
        "audience": Setting(default="agent"),
    },
)
KEYCLOAK = Adapter(
    "identity",
    "keycloak",
    _keycloak,
    {
        "issuer_url": Setting(required=True),
        "audience": Setting(required=True),
        "client_id": Setting(),
        "client_secret": Setting(secret=True),
        "far_end_audience": Setting(),
        "scope": Setting(),
    },
)
ENTRA_ID = Adapter(
    "identity",
    "entra-id",
    _entra,
    {
        "tenant": Setting(required=True),
        "audience": Setting(required=True),
        "client_id": Setting(),
        "client_secret": Setting(secret=True),
        "far_end_scope": Setting(),
    },
)

__all__ = ["ENTRA_ID", "KEYCLOAK", "LOCAL_DEV", "Sessions"]
