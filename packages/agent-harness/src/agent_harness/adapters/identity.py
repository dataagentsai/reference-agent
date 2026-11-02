"""The `identity` port: whose sessions the agent accepts, and the token exchange.

    local-dev   sessions signed in this process (`identity.local`); the only
                adapter that can mint, so a sign-in page exists only here
    keycloak    the Open Stack's realm: its published keys, RFC 8693 exchange
    entra-id    Entra's v2.0 issuer for the tenant, Entra's claim shapes, and
                the on-behalf-of grant; `role_scopes` maps an app role (`roles`) to
                the scopes it grants, e.g. `desk.handler` to the desk's four;
                `holder_claim` names the claim the customer id is read from; with
                `login_redirect_uri` (and `session_key`) the browser sign-in,
                `entra_login.EntraLogin`, and the sealer for its cookies
    cognito     a Cognito user pool's access tokens (`identity.cognito`): the app
                client in `client_id`, `scope` as a string, `cognito:groups`
                through `role_scopes`. Cognito has no on-behalf-of grant, so with
                `far_end_audience` the agent's own issuer signs the far end's
                token with `signing_key` (one RSA key in Secrets Manager, shared
                by every task) and `jwks` is what the app publishes; with
                `login_redirect_uri` the pool's browser sign-in
                (`cognito.CognitoLogin`) and the sealer for its cookies

The product is `Sessions`: the issuer, the verifier that reads this issuer's
claim shapes (Entra's `scp` is a string, Keycloak's a list), the exchange when
one is configured, the approval worker's own login for the far end when one is
configured (`worker`), and the signer where there is one.

The far end's token (A1): `local-dev` with `far_end_audience` exchanges a
session by minting a short-lived token for that audience (`LocalExchange`), and
with `worker_scopes` gives the worker a login with no holder; `entra-id` with
a client id and secret uses the on-behalf-of grant, and with `worker_scope` the
client-credentials grant for the worker. The hook `keys` replaces
a remote key set with a local one, which is how the contract tests run with no
network; `transport`, the token endpoint's answers, for the Entra grants.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_harness import identity as ident
from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring, sealing_key
from agent_harness.identity.local import KID, LocalIssuer

if TYPE_CHECKING:
    from agent_harness.identity.entra_login import EntraLogin, Sealer


@dataclass(frozen=True)
class Sessions:
    issuer: ident.Issuer
    verify: ident.Verifier
    """This issuer's verifier: what every edge checks a session with (F-30)."""
    exchange: ident.Exchange | None = None
    signer: LocalIssuer | None = None
    worker: ident.Exchange | None = None
    """The approval worker's login for the far end: no person's session is
    there when a handler decides later (A1)."""
    login: EntraLogin | None = None
    """The browser sign-in (code flow + PKCE + nonce), where the overlay gives
    one (A2): an edge mounts its routes only then, as `/signin` only with a signer."""
    sealer: Sealer | None = None
    """Seals what the sign-in's cookies carry; present exactly when `login` is."""
    jwks: dict[str, Any] | None = None
    """The public keys of the agent's own issuer for far-end tokens, which the app
    publishes at `/.well-known/jwks.json` (`cognito`). Unlike `signer`, it opens
    no sign-in page: this adapter can mint for the far end, never a session."""


@asynccontextmanager
async def _local(wiring: Wiring) -> AsyncIterator[Sessions]:
    from agent_harness.identity.local import LocalExchange, LocalWorkerLogin

    settings = wiring.settings
    kid = f"{KID}-{uuid.uuid4().hex[:8]}"  # a new key each start, so a new id
    signer = LocalIssuer(url=str(settings["url"]), audience=str(settings["audience"]), kid=kid)
    issuer = signer.issuer()
    far, ttl = settings.get("far_end_audience"), int(settings["far_end_ttl_s"])
    party = str(settings["party"])
    exchange: ident.Exchange | None = LocalExchange(signer, str(far), party, ttl) if far else None
    worker: ident.Exchange | None = None
    if far and settings.get("worker_scopes"):
        scopes = frozenset(str(s) for s in settings["worker_scopes"])
        worker = LocalWorkerLogin(signer, str(far), scopes, f"{party}-workflow", ttl)
    yield Sessions(issuer, ident.verifier(issuer), exchange, signer, worker)


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
    yield Sessions(issuer, ident.verifier(issuer), exchange)


@asynccontextmanager
async def _entra(wiring: Wiring) -> AsyncIterator[Sessions]:
    from agent_harness.identity import entra

    settings = wiring.settings
    tenant = str(settings["tenant"])
    issuer = entra.entra_issuer(tenant, str(settings["audience"]), keys=wiring.hooks.get("keys"))
    exchange: ident.Exchange | None = None
    worker: ident.Exchange | None = None
    client_id, secret = settings.get("client_id"), settings.get("client_secret")
    http = wiring.hooks.get("transport")  # the token endpoint answered in process
    if client_id and secret:
        exchange = entra.EntraOnBehalfOf(
            tenant,
            client_id=str(client_id),
            scope=str(settings["far_end_scope"]),
            client_secret=str(secret),
            transport=http,
        )
        if settings.get("worker_scope"):
            worker = entra.EntraClientCredentials(
                tenant,
                client_id=str(client_id),
                scope=str(settings["worker_scope"]),
                client_secret=str(secret),
                transport=http,
            )
    roles = role_scopes(settings.get("role_scopes"))
    holder = str(settings["holder_claim"])

    def verify(token: str) -> ident.Principal:
        return entra.verify_entra(token, issuer=issuer, role_scopes=roles, holder_claim=holder)

    login, sealer = _login(wiring, tenant, issuer)
    yield Sessions(issuer, verify, exchange, worker=worker, login=login, sealer=sealer)


@asynccontextmanager
async def _cognito(wiring: Wiring) -> AsyncIterator[Sessions]:
    from agent_harness.identity import cognito
    from agent_harness.identity.local import LocalWorkerLogin

    settings = wiring.settings
    issuer = cognito.cognito_issuer(
        str(settings["region"]),
        str(settings["user_pool_id"]),
        str(settings["audience"]),
        keys=wiring.hooks.get("keys"),
    )
    roles = role_scopes(settings.get("role_scopes"))
    holder = str(settings["holder_claim"])

    def verify(token: str) -> ident.Principal:
        return cognito.verify_cognito(token, issuer=issuer, role_scopes=roles, holder_claim=holder)

    login, sealer = _cognito_login(wiring, issuer)
    far = settings.get("far_end_audience")
    if not far:
        yield Sessions(issuer, verify, login=login, sealer=sealer)
        return
    if not settings.get("signing_key") or not settings.get("token_issuer_url"):
        raise OverlayRefused(
            "cognito: far_end_audience needs signing_key (a reference) and token_issuer_url"
        )
    signer = cognito.agent_issuer(str(settings["token_issuer_url"]), str(settings["signing_key"]))
    party, ttl = str(settings["party"]), int(settings["far_end_ttl_s"])
    exchange = cognito.CognitoExchange(verify, signer, str(far), party, ttl)
    worker: ident.Exchange | None = None
    if settings.get("worker_scopes"):
        scopes = frozenset(str(s) for s in settings["worker_scopes"])
        worker = LocalWorkerLogin(signer, str(far), scopes, f"{party}-workflow", ttl)
    yield Sessions(
        issuer, verify, exchange, worker=worker, login=login, sealer=sealer, jwks=signer.jwks
    )


def _cognito_login(wiring: Wiring, issuer: ident.Issuer) -> tuple[EntraLogin | None, Sealer | None]:
    """The user pool's browser sign-in, when the overlay gives a redirect URI:
    the pool's domain, the app client's id and secret, and a cookie key."""
    settings = wiring.settings
    redirect = settings.get("login_redirect_uri")
    if not redirect:
        return None, None
    domain, secret, key = (
        settings.get("login_domain"),
        settings.get("client_secret"),
        settings.get("session_key"),
    )
    if not (domain and secret and key):
        raise OverlayRefused(
            "cognito: login_redirect_uri needs login_domain, client_secret and session_key"
        )
    from agent_harness.identity.cognito import CognitoLogin
    from agent_harness.identity.entra_login import Sealer

    login = CognitoLogin(
        str(domain),
        issuer=issuer,
        client_id=issuer.audience,
        client_secret=str(secret),
        redirect_uri=str(redirect),
        scopes=tuple(str(s) for s in settings.get("login_scopes") or ()),
        transport=wiring.hooks.get("transport"),
    )
    return login, Sealer(sealing_key(str(key), "identity.sign-in-cookies"))


def _login(
    wiring: Wiring, tenant: str, issuer: ident.Issuer
) -> tuple[EntraLogin | None, Sealer | None]:
    """The browser sign-in, when the overlay gives a redirect URI (A2): the app's
    own client id and secret, and a key for the cookies, or a refusal at startup."""
    settings = wiring.settings
    redirect = settings.get("login_redirect_uri")
    if not redirect:
        return None, None
    client_id, secret = settings.get("client_id"), settings.get("client_secret")
    key = settings.get("session_key")
    if not (client_id and secret and key):
        raise OverlayRefused(
            "entra-id: login_redirect_uri needs client_id, client_secret and session_key"
        )
    from agent_harness.identity.entra_login import EntraLogin, Sealer

    login = EntraLogin(
        tenant,
        issuer=issuer,
        client_id=str(client_id),
        client_secret=str(secret),
        redirect_uri=str(redirect),
        scopes=tuple(str(s) for s in settings.get("login_scopes") or ()),
        transport=wiring.hooks.get("transport"),
    )
    return login, Sealer(sealing_key(str(key), "identity.sign-in-cookies"))


def role_scopes(value: Any) -> dict[str, tuple[str, ...]]:
    """`role_scopes` from the overlay: a mapping of role to a list of scopes, or
    a refusal at startup — a misspelt shape must not silently grant nothing."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(scopes, list) and all(isinstance(s, str) for s in scopes)
        for scopes in value.values()
    ):
        raise OverlayRefused("entra-id: role_scopes maps each app role to a list of scopes")
    return {str(role): tuple(scopes) for role, scopes in value.items()}


LOCAL_DEV = Adapter(
    "identity",
    "local-dev",
    _local,
    {
        "url": Setting(default="http://local-issuer.test/realms/agent"),
        "audience": Setting(default="agent"),
        "far_end_audience": Setting(),
        "far_end_ttl_s": Setting(default=300),
        "party": Setting(default="agent"),
        "worker_scopes": Setting(),
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
        "worker_scope": Setting(),
        "role_scopes": Setting(),
        "holder_claim": Setting(default=ident.CLAIM_CUSTOMER),
        "login_redirect_uri": Setting(),
        "login_scopes": Setting(),
        "session_key": Setting(secret=True),
    },
)

COGNITO = Adapter(
    "identity",
    "cognito",
    _cognito,
    {
        "region": Setting(required=True),
        "user_pool_id": Setting(required=True),
        "audience": Setting(required=True),
        "role_scopes": Setting(),
        "holder_claim": Setting(default=ident.CLAIM_CUSTOMER),
        "far_end_audience": Setting(),
        "token_issuer_url": Setting(),
        "signing_key": Setting(secret=True),
        "far_end_ttl_s": Setting(default=300),
        "party": Setting(default="agent"),
        "worker_scopes": Setting(),
        "login_domain": Setting(),
        "client_secret": Setting(secret=True),
        "login_redirect_uri": Setting(),
        "login_scopes": Setting(),
        "session_key": Setting(secret=True),
    },
)

__all__ = ["COGNITO", "ENTRA_ID", "KEYCLOAK", "LOCAL_DEV", "Sessions", "role_scopes"]
