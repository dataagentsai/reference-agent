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
from starlette.testclient import TestClient

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.contracts import ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
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
