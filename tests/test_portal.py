"""T-026 (B): the portal logs a customer in and keeps the login server-side.

Driven through the real Starlette app, with the issuer's code flow played by
`evals.issuer.LocalLogin`, which holds the PKCE challenge the way a real issuer
does. What these hold: no token of any kind reaches the page; the widget's
identity is the login and its HMAC; a staff login is never kept; and logout
reaches the issuer, the store and the agent's cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

import pytest
from evals import issuer as issuing
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from support_agent import portal as ptl
from support_agent import telemetry as tel
from support_agent.state import InMemorySessionStore

HMAC_TOKEN = "inbox-identity-secret"
WIDGET = ptl.Widget(
    base_url="http://chat.local.test", website_token="web-token", hmac_token=HMAC_TOKEN
)


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


class Rig:
    def __init__(self) -> None:
        self.login = issuing.LocalLogin()
        self.sessions = InMemorySessionStore()
        self.forgotten: list[str] = []
        portal = ptl.Portal(
            login=self.login,
            issuer=issuing.issuer(),
            sessions=self.sessions,
            widget=WIDGET,
            redirect_uri="http://testserver/portal/callback",
            base_path="/portal",
            cookie_key=b"portal-cookie-key-for-tests-only",
            secure_cookies=False,
            forget=self.forgotten.append,
        )
        app = Starlette(routes=[Mount("/portal", app=ptl.build(portal))])
        self.client = TestClient(app, follow_redirects=False)

    def start(self) -> str:
        res = self.client.get("/portal/login")
        assert res.status_code == 303
        return res.headers["location"]

    def sign_in(self, customer_id: str | None = "C-1042"):
        back = self.login.approve(self.start(), customer_id)
        return self.client.get("/portal/callback", params=back)


@pytest.fixture
def rig() -> Rig:
    return Rig()


def kept(rig: Rig, subject: str):
    return asyncio.run(rig.sessions.get(subject))


@pytest.mark.discharges("AHC-0099")
def test_a_visitor_with_no_login_is_sent_to_log_in(rig: Rig) -> None:
    res = rig.client.get("/portal/")
    assert (res.status_code, res.headers["location"]) == (303, "/portal/login")


@pytest.mark.discharges("AHC-0099")
def test_login_goes_to_the_issuer_with_pkce_and_a_state(rig: Rig) -> None:
    location = rig.start()
    assert location.startswith(issuing.LocalLogin.AUTHORIZE)
    assert "code_challenge=" in location and "state=" in location
    flow = rig.client.cookies.get(ptl.FLOW_COOKIE)
    assert flow and "verifier" not in flow, "the verifier is inside a signed payload, not bare"


@pytest.mark.discharges("AHC-0099", "AAC-0111")
def test_a_customer_login_is_kept_and_the_page_carries_no_token(rig: Rig) -> None:
    res = rig.sign_in("C-1042")
    assert (res.status_code, res.headers["location"]) == (303, "/portal/")
    stored = kept(rig, "login-C-1042")
    assert stored is not None

    page = rig.client.get("/portal/").text
    expected = hmac.new(HMAC_TOKEN.encode(), b"login-C-1042", hashlib.sha256).hexdigest()
    assert '"identifier": "login-C-1042"' in page
    assert f'"identifier_hash": "{expected}"' in page
    for secret in (stored.refresh_token, HMAC_TOKEN, "eyJ"):
        assert secret not in page, "no credential reaches the page"


def _tamper(rig: Rig, back: dict[str, str]) -> dict[str, str]:
    return {**back, "state": "not-the-state"}


def _wrong_verifier(rig: Rig, back: dict[str, str]) -> dict[str, str]:
    """A code stolen from one flow, redeemed in another with its own verifier."""
    other = rig.login.approve(rig.start(), "C-9999")
    return {"code": back["code"], "state": other["state"]}


def _no_flow_cookie(rig: Rig, back: dict[str, str]) -> dict[str, str]:
    rig.client.cookies.delete(ptl.FLOW_COOKIE)
    return back


# (why, the customer who logs in, how the return is interfered with, status)
REFUSALS = [
    ("a state that is not the one sent", "C-1042", _tamper, 400),
    ("no flow cookie to check the state against", "C-1042", _no_flow_cookie, 400),
    ("a code redeemed with another flow's verifier", "C-1042", _wrong_verifier, 400),
    ("a staff login, which has no customer", None, None, 403),
]


@pytest.mark.parametrize(
    ("why", "customer", "interfere", "status"), REFUSALS, ids=[r[0] for r in REFUSALS]
)
@pytest.mark.discharges("AHC-0099", "AAC-0111")
def test_a_login_that_does_not_complete_keeps_nothing(
    rig: Rig, why: str, customer, interfere, status: int
) -> None:
    back = rig.login.approve(rig.start(), customer)
    if interfere is not None:
        back = interfere(rig, back)
    res = rig.client.get("/portal/callback", params=back)

    assert res.status_code == status
    assert ptl.SESSION_COOKIE not in rig.client.cookies
    for subject in ("login-C-1042", "login-C-9999", "login-staff"):
        assert kept(rig, subject) is None, "nothing is kept for a login that did not complete"
    if customer is None:
        assert rig.login.ended, "a staff login is ended at the issuer, not left open"


@pytest.mark.discharges("AHC-0099")
def test_a_tampered_session_cookie_is_no_session(rig: Rig) -> None:
    rig.sign_in("C-1042")
    value = rig.client.cookies.get(ptl.SESSION_COOKIE)
    rig.client.cookies.set(ptl.SESSION_COOKIE, value[:-2] + "00", path="/portal")
    assert rig.client.get("/portal/").status_code == 303


@pytest.mark.discharges("AHC-0099")
def test_logout_reaches_the_issuer_the_store_and_the_agent(rig: Rig) -> None:
    rig.sign_in("C-1042")
    stored = kept(rig, "login-C-1042")

    res = rig.client.post("/portal/logout")

    assert res.status_code == 303
    assert rig.login.ended == [stored.refresh_token]
    assert kept(rig, "login-C-1042") is None
    assert rig.forgotten == ["login-C-1042"]
    assert rig.client.get("/portal/").status_code == 303


@pytest.mark.discharges("AHC-0099")
def test_a_session_cookie_outlives_nothing_once_the_login_is_gone(rig: Rig) -> None:
    """Logged out somewhere else: the cookie still verifies, and the page still
    refuses, because the stored login is what counts."""
    rig.sign_in("C-1042")
    asyncio.run(rig.sessions.delete("login-C-1042"))
    assert rig.client.get("/portal/").status_code == 303


def test_an_expired_session_cookie_is_no_session() -> None:
    rig = Rig()
    rig.sign_in("C-1042")
    portal = rig.client.app.routes[0].app.state.portal
    later = ptl.Portal(**{**portal.__dict__, "clock": lambda: time.time() + ptl.SESSION_TTL_S + 1})
    rig.client.app.routes[0].app.state.portal = later
    assert rig.client.get("/portal/").status_code == 303
