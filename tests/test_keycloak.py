"""T-002: the agent against the composed Keycloak realm, not a stand-in for it.

The offline suite signs with `evals/issuer.py`, which issues what the realm is
supposed to issue. These tests check that it does: the same users, fetched from
the real token endpoint, verified through the realm's published keys, and run
through the chat edge and the reviewer desk. They skip when no Keycloak is
running, the way the database tests do.

What they would have caught: the first import of `deploy/keycloak` produced
tokens with no `sub`, because a realm that lists its own client scopes does not
get Keycloak's default `basic` scope. Every session would have been refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import pytest
from agenttwin import Live, load, project
from evals import durable
from starlette.testclient import TestClient

from order_system import authoriser
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.binding import SCOPES
from support_agent.contracts import IdempotencyKey, Identity, ModelResponse, RunId, StoredSession
from support_agent.idempotency import InMemoryLedger
from support_agent.identity import sessions
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore, InMemorySessionStore
from support_agent.tools import connect

REALM = os.environ.get("AGENT_TEST_ISSUER_URL", "http://localhost:8080/realms/support")
BASE = REALM.split("/realms/")[0]
PASSWORD = "local-dev-only"
WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@cache
def _issuer() -> ident.Issuer | None:
    try:
        keys = ident.RemoteJWKS.discover(REALM)
    except (urllib.error.URLError, OSError):
        return None
    return ident.Issuer(url=REALM, audience="support-agent", keys=keys)


@pytest.fixture
def issuer() -> ident.Issuer:
    found = _issuer()
    if found is None:
        pytest.skip(f"no Keycloak realm at {REALM}")
    return found


def post_form(url: str, fields: dict[str, str]) -> dict[str, object]:
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as response:
        return json.load(response)


def login(username: str) -> str:
    """A session from the realm's own token endpoint, as the chat client."""
    body = post_form(
        f"{REALM}/protocol/openid-connect/token",
        {
            "grant_type": "password",
            "client_id": "support-chat",
            "username": username,
            "password": PASSWORD,
        },
    )
    return str(body["access_token"])


# (user, the customer it must speak for, scopes it must hold, scopes it must not)
USERS = [
    ("c-1042", "C-1042", ident.CUSTOMER_SCOPES, ident.REVIEWER_SCOPES),
    ("c-9999", "C-9999", ident.CUSTOMER_SCOPES, ident.REVIEWER_SCOPES),
    ("desk-1", None, ident.REVIEWER_SCOPES, ident.CUSTOMER_SCOPES),
]


@pytest.mark.parametrize(("user", "customer", "holds", "lacks"), USERS, ids=[u[0] for u in USERS])
@pytest.mark.discharges("AHC-0099", "AAC-0057")
def test_the_realm_issues_what_the_agent_requires(
    issuer: ident.Issuer, user: str, customer: str | None, holds, lacks
) -> None:
    principal = ident.verify(login(user), issuer=issuer)

    assert principal.customer_id == customer
    assert principal.subject and principal.subject != customer, "sub is the login, not the row"
    assert principal.session, "jti names the session"
    assert holds <= principal.scopes
    assert not (lacks & principal.scopes)
    assert ident.SCOPE_REFUNDS_WRITE not in principal.scopes, "nobody holds refunds:write"


@pytest.mark.discharges("P-OWNERSHIP", "AAC-0057")
def test_only_an_administrator_can_link_a_login_to_a_customer(issuer: ident.Issuer) -> None:
    """If a customer could edit `customer_id` on their own account, the claim
    the ownership rule trusts would be theirs to choose."""
    admin = post_form(
        f"{BASE}/realms/master/protocol/openid-connect/token",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", PASSWORD),
        },
    )["access_token"]
    request = urllib.request.Request(
        f"{BASE}/admin/realms/support/users/profile",
        headers={"Authorization": f"Bearer {admin}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        profile = json.load(response)
    attribute = next(a for a in profile["attributes"] if a["name"] == ident.CLAIM_CUSTOMER)
    assert attribute["permissions"] == {"view": ["admin"], "edit": ["admin"]}


@pytest.fixture
def app(issuer: ident.Issuer):
    """The chat edge and the desk, accepting only the realm's sessions."""
    world = Live.start(load(WORLD))
    escalations = esc.InMemoryEscalationStore()

    @asynccontextmanager
    async def make_agent():
        async with connect(project(world), ledger=InMemoryLedger()) as tools:
            yield ep.build(
                llm=ScriptedClient([ModelResponse(text="Looking now.")] * 20),
                tools=tools,
                store=InMemoryCheckpointStore(),
                escalations=escalations,
            )

    with TestClient(serve.build(make_agent, issuer=issuer, escalations=escalations)) as client:
        yield client


# (user, route, status) — the edge decides from the realm's session alone
ROUTES = [
    ("c-1042", "chat", 200),
    ("desk-1", "chat", 403),
    ("desk-1", "ops", 200),
    ("c-1042", "ops", 403),
]


@pytest.mark.parametrize(
    ("user", "route", "status"), ROUTES, ids=[f"{u} {r}" for u, r, _ in ROUTES]
)
@pytest.mark.discharges("AHC-0099", "AAC-0111", "P-ESC-OWNS")
def test_a_realm_session_reaches_only_its_own_surface(
    app, user: str, route: str, status: int
) -> None:
    headers = {"authorization": f"Bearer {login(user)}"}
    if route == "chat":
        res = app.post("/chat", json={"text": "where is AB-10001"}, headers=headers)
    else:
        res = app.get("/ops/escalations", headers=headers)
    assert res.status_code == status, res.text


def test_an_expired_realm_session_is_refused(issuer: ident.Issuer) -> None:
    token = login("c-1042")
    with pytest.raises(ident.InvalidSession):
        ident.verify(token, issuer=issuer, now=int(time.time()) + 3600)


# --------------------------------------------------------------------------- #
# T-002 (C): the realm's token exchange, and the far end that believes it.
# --------------------------------------------------------------------------- #


def exchange() -> sessions.TokenExchange:
    return sessions.TokenExchange.discover(
        REALM,
        client_id="support-agent",
        client_secret=os.environ.get("SUPPORT_AGENT_CLIENT_SECRET", "local-dev-only-agent-secret"),
        audience="order-system",
        scope="order-system-audience",
    )


def customer(user: str, issuer: ident.Issuer) -> Identity:
    return ident.verify(login(user), issuer=issuer).as_customer()


@pytest.mark.discharges("AAC-0057", "AHC-0099")
async def test_the_realm_exchanges_a_session_for_the_order_system(issuer: ident.Issuer) -> None:
    token = await exchange().for_far_end(customer("c-1042", issuer))
    far_end = ident.Issuer(url=REALM, audience="order-system", keys=issuer.keys)
    principal = ident.verify(token, issuer=far_end)

    assert (principal.customer_id, principal.party) == ("C-1042", "support-agent")
    with pytest.raises(ident.InvalidSession):
        ident.verify(token, issuer=issuer)  # addressed to the order system, not the agent


# (user, whether the far end serves C-1042's order AB-10003 to them)
FAR_END = [("c-1042", True), ("c-9999", False)]


@pytest.mark.parametrize(("user", "served"), FAR_END, ids=[u for u, _ in FAR_END])
@pytest.mark.discharges("P-OWNERSHIP", "AAC-0057")
async def test_the_far_end_serves_the_realms_customer_and_nobody_else(
    issuer: ident.Issuer, user: str, served: bool
) -> None:
    world = Live.start(load(WORLD))
    check = authoriser(
        issuer=ident.Issuer(url=REALM, audience="order-system", keys=issuer.keys),
        approvals=durable.Remembered(),
        required_scopes=SCOPES,
        clock=lambda: int(time.time()),
    )
    server = project(world, scopes=SCOPES, authorise=check)
    key = IdempotencyKey(run_id=RunId("run_kc"), step=1, iteration=0)
    async with connect(server, ledger=InMemoryLedger(), exchange=exchange()) as tools:
        result = await tools.call("get_order", {"id": "AB-10003"}, customer(user, issuer), key)
    assert (not result.is_error) is served, result.text


# --------------------------------------------------------------------------- #
# T-026 (A): the portal's stored login, and logout reaching the agent.
# --------------------------------------------------------------------------- #

PORTAL = (
    "support-portal",
    os.environ.get("SUPPORT_PORTAL_CLIENT_SECRET", "local-dev-only-portal-secret"),
)


@pytest.mark.discharges("AHC-0099", "P-OWNERSHIP")
async def test_a_portal_login_is_resumed_until_the_customer_logs_out(issuer: ident.Issuer) -> None:
    client_id, secret = PORTAL
    login_body = post_form(
        f"{REALM}/protocol/openid-connect/token",
        {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": secret,
            "username": "c-1042",
            "password": PASSWORD,
        },
    )
    subject = ident.verify(str(login_body["access_token"]), issuer=issuer).subject
    store = InMemorySessionStore()
    await store.put(
        StoredSession(subject=subject, refresh_token=str(login_body["refresh_token"]), updated_at=0)
    )
    grant = sessions.KeycloakRefresh.discover(REALM, client_id=client_id, client_secret=secret)
    resumed = sessions.Resume(store, grant, issuer=issuer)

    assert (await resumed.identity_for(subject)).customer_id == "C-1042"

    stored = await store.get(subject)
    assert stored is not None
    _logout(client_id, secret, stored.refresh_token)
    resumed.forget(subject)
    with pytest.raises(ident.SessionEnded):
        await resumed.identity_for(subject)
    assert await store.get(subject) is None


def _logout(client_id: str, secret: str, refresh_token: str) -> None:
    """The realm's logout returns 204 with no body, which `post_form` would try to read."""
    data = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": secret, "refresh_token": refresh_token}
    ).encode()
    request = urllib.request.Request(f"{REALM}/protocol/openid-connect/logout", data=data)
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status in (200, 204)


# --------------------------------------------------------------------------- #
# T-026 (B): the portal's code flow against the realm's real login form.
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _log_in_at_the_realm(authorize_url: str, username: str) -> dict[str, str]:
    """What a person does in a browser: open the login page, submit the form, and
    come back with a code. Returns the query the realm redirects to.

    The realm's login cookies are `Secure` (they are `SameSite=None`), which a
    browser still sends to http://localhost and Python's cookie jar does not, so
    they are carried by hand.
    """
    import re

    with urllib.request.urlopen(authorize_url, timeout=10) as response:
        cookies = "; ".join(c.split(";")[0] for c in response.headers.get_all("Set-Cookie") or [])
        form = response.read().decode()
    action = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', form)
    assert action, "the realm's login form"
    submit = urllib.request.Request(
        action.group(1).replace("&amp;", "&"),
        data=urllib.parse.urlencode({"username": username, "password": PASSWORD}).encode(),
        headers={"Cookie": cookies},
    )
    try:
        urllib.request.build_opener(_NoRedirect).open(submit, timeout=10)
    except urllib.error.HTTPError as redirect:
        location = redirect.headers["Location"]
    else:
        raise AssertionError("the realm did not redirect back after login")
    assert location, "a redirect with somewhere to go"
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))


@pytest.mark.discharges("AHC-0099", "AAC-0111")
async def test_the_portal_logs_a_customer_in_through_the_realm(issuer: ident.Issuer) -> None:
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from support_agent import portal as ptl

    client_id, secret = PORTAL
    realm_login = sessions.KeycloakLogin.discover(REALM, client_id=client_id, client_secret=secret)
    store = InMemorySessionStore()
    portal = ptl.Portal(
        login=realm_login,
        issuer=issuer,
        sessions=store,
        widget=ptl.Widget(base_url="http://localhost:3100", website_token="w", hmac_token="h"),
        redirect_uri="http://localhost:8077/portal/callback",
        base_path="/portal",
        cookie_key=b"portal-cookie-key-for-tests-only",
        secure_cookies=False,
    )
    app = Starlette(routes=[Mount("/portal", app=ptl.build(portal))])
    with TestClient(app, follow_redirects=False) as client:
        authorize = client.get("/portal/login").headers["location"]
        back = await asyncio.to_thread(_log_in_at_the_realm, authorize, "c-1042")
        assert client.get("/portal/callback", params=back).status_code == 303
        page = client.get("/portal/")

        assert page.status_code == 200
        subject = ident.verify(login("c-1042"), issuer=issuer).subject
        assert await store.get(subject) is not None, "the login is kept under its sub"
        grant = sessions.KeycloakRefresh.discover(REALM, client_id=client_id, client_secret=secret)
        resumed = sessions.Resume(store, grant, issuer=issuer)
        assert (await resumed.identity_for(subject)).customer_id == "C-1042"

        assert client.post("/portal/logout").status_code == 303
        resumed.forget(subject)
        with pytest.raises(ident.SessionEnded):
            await resumed.identity_for(subject)
