"""A Cognito user pool's browser sign-in, held to the Entra sign-in's rows (A2, T-123).

The same flow (`entra_login.Flow`: state, PKCE S256, nonce) against the pool's
own domain, with Cognito's differences: no `offline_access`, the client proves
itself with HTTP Basic, the user is `sub` in both tokens, and the access token
names the app client in `client_id`, never `aud`. The pool's token endpoint is
answered in process: no network, no account.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
from functools import cache
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_harness.adapters import ADAPTERS, OverlayRefused, Wiring
from agent_harness.identity import JWKS, InvalidSession
from agent_harness.identity.cognito import CognitoLogin, cognito_issuer, verify_cognito
from agent_harness.identity.entra_login import Flow, Tokens

REGION, POOL, CLIENT = "ap-south-1", "ap-south-1_LOGIN1", "fnol-app-client"
ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
DOMAIN = "https://fnol-test.auth.ap-south-1.amazoncognito.com"
REDIRECT = "https://agent.test/signin/callback"
KID = "cognito-login-key"
HOLDER = "customer_id"
BASIC = "Basic " + base64.b64encode(f"{CLIENT}:s3cret".encode()).decode()


@cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _keys() -> JWKS:
    public = RSAAlgorithm.to_jwk(_key().public_key(), as_dict=True)
    return JWKS({"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]})


def _signed(**claims: Any) -> str:
    return jwt.encode(claims, _key(), algorithm="RS256", headers={"kid": KID})


def _access(**over: Any) -> str:
    now = int(time.time())
    base = {"iss": ISS, "client_id": CLIENT, "token_use": "access", "sub": "s-rohan"}
    return _signed(
        **{
            **base,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + 3600,
            HOLDER: "PH-1001",
            **over,
        }
    )


def _id_token(nonce: str, **over: Any) -> str:
    now = int(time.time())
    base = {"iss": ISS, "aud": CLIENT, "token_use": "id", "sub": "s-rohan", "nonce": nonce}
    return _signed(**{**base, "iat": now, "exp": now + 3600, **over})


class FakeTokenEndpoint:
    """The pool's `/oauth2/token`: Basic client auth; a code good once, for its challenge."""

    def __init__(self, access: dict[str, Any] | None = None) -> None:
        self.codes: dict[str, tuple[str, str]] = {}
        self.access = access or {}
        self.posted: list[dict[str, str]] = []

    def authorize(self, url: str) -> str:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = uuid.uuid4().hex
        self.codes[code] = (query["code_challenge"], query["nonce"])
        return code

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.posted.append(form)
        if request.headers.get("authorization") != BASIC or "client_secret" in form:
            return httpx.Response(400, json={"error": "invalid_client"})
        if form["grant_type"] == "refresh_token":
            if form["refresh_token"] != "rt-1":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": _access(**self.access)})
        challenge, nonce = self.codes.pop(form.get("code", ""), ("", ""))
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if not challenge or base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        body = {
            "access_token": _access(**self.access),
            "refresh_token": "rt-1",
            "id_token": _id_token(nonce),
        }
        return httpx.Response(200, json=body)


def _issuer() -> Any:
    return cognito_issuer(REGION, POOL, CLIENT, keys=_keys())


def _login(endpoint: FakeTokenEndpoint) -> CognitoLogin:
    return CognitoLogin(
        DOMAIN,
        issuer=_issuer(),
        client_id=CLIENT,
        client_secret="s3cret",
        redirect_uri=REDIRECT,
        scopes=("claims-fnol/claims.read",),
        transport=httpx.MockTransport(endpoint.handle),
    )


def _verify(token: str) -> Any:
    return verify_cognito(token, issuer=_issuer(), holder_claim=HOLDER)


@pytest.mark.discharges("AAC-0057", "AHC-0099")
def test_the_authorize_url_is_the_pools_with_state_nonce_and_an_s256_challenge() -> None:
    flow = Flow()
    url = urlsplit(_login(FakeTokenEndpoint()).authorize_url(flow))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}" == DOMAIN and url.path == "/oauth2/authorize"
    assert query["state"] == flow.state and query["nonce"] == flow.nonce
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == flow.challenge != flow.verifier
    assert query["scope"].split() == ["openid", "profile", "claims-fnol/claims.read"]
    assert query["redirect_uri"] == REDIRECT and "client_secret" not in query


SIGN_IN = [
    # (row, what changes, the holder signed in, or the refusal's words)
    ("a good code exchange", {}, "PH-1001"),
    ("the PKCE verifier is not this flow's", {"verifier": "someone-elses"}, "invalid_grant"),
    ("the id token's nonce is not this flow's", {"nonce": "replayed"}, "nonce"),
    (
        "the access token is for another app client",
        {"client_id": "another-client"},
        "another app client",
    ),
    ("the id token names another user", {"id_sub": "s-meera"}, "different users"),
    ("no id token came back", {"no_id": True}, "no id token"),
    ("a code used twice", {"twice": True}, "invalid_grant"),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "change", "outcome"), SIGN_IN, ids=[r[0] for r in SIGN_IN])
def test_a_sign_in(row: str, change: dict[str, Any], outcome: str) -> None:
    endpoint = FakeTokenEndpoint(
        {"client_id": change["client_id"]} if "client_id" in change else None
    )
    login = _login(endpoint)
    flow = Flow()
    code = endpoint.authorize(login.authorize_url(flow))

    async def run() -> Any:
        sent = Flow(flow.state, change.get("verifier", flow.verifier), flow.nonce)
        if change.get("twice"):
            await login.redeem(code, sent)
        tokens = await login.redeem(code, sent)
        if "id_sub" in change or change.get("no_id"):
            other = None if change.get("no_id") else _id_token(flow.nonce, sub=change["id_sub"])
            tokens = Tokens(tokens.access, tokens.refresh, other)
        checked = Flow(flow.state, flow.verifier, change.get("nonce", flow.nonce))
        return login.signed_in(tokens, checked, _verify)

    if outcome.startswith("PH-"):
        assert asyncio.run(run()).customer_id == outcome
        assert endpoint.posted[-1]["code_verifier"] == flow.verifier
    else:
        with pytest.raises(InvalidSession, match=outcome):
            asyncio.run(run())


REFRESH = [
    ("a kept refresh token gives a fresh session", "rt-1", "PH-1001"),
    ("a revoked one is refused", "rt-revoked", "refresh refused: invalid_grant"),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "stored", "outcome"), REFRESH, ids=[r[0] for r in REFRESH])
def test_a_refresh(row: str, stored: str, outcome: str) -> None:
    login = _login(FakeTokenEndpoint())
    if outcome.startswith("PH-"):
        access, keep = asyncio.run(login.refresh(stored))
        assert _verify(access).customer_id == outcome
        assert keep == stored, "Cognito does not rotate it by default, so the old one is kept"
    else:
        with pytest.raises(InvalidSession, match=outcome):
            asyncio.run(login.refresh(stored))


def test_logging_out_ends_the_pools_session_too() -> None:
    url = urlsplit(_login(FakeTokenEndpoint()).logout_url("https://agent.test/"))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.path == "/logout"
    assert query == {"client_id": CLIENT, "logout_uri": "https://agent.test/"}


BASE = {"region": REGION, "user_pool_id": POOL, "audience": CLIENT}
LOGIN = {
    "login_domain": DOMAIN,
    "client_secret": "s3cret",
    "session_key": "k",
    "login_redirect_uri": REDIRECT,
}
ADAPTER = [
    # (row, the overlay's settings beyond the pool, built: True, False, or the refusal)
    ("no redirect URI, no sign-in", {}, False),
    ("a redirect URI with what it needs builds the sign-in", LOGIN, True),
    ("a redirect URI without a domain is refused", {**LOGIN, "login_domain": None}, "login_domain"),
]


@pytest.mark.parametrize(("row", "extra", "built"), ADAPTER, ids=[r[0] for r in ADAPTER])
def test_the_cognito_adapter_builds_the_sign_in(
    row: str, extra: dict[str, Any], built: Any
) -> None:
    adapter = ADAPTERS.load("identity", "cognito")
    settings = {k: s.default for k, s in adapter.settings.items()} | BASE | extra

    async def run() -> Any:
        async with adapter.build(Wiring(settings, {}, {"keys": _keys()}, "test")) as sessions:
            return sessions

    if isinstance(built, str):
        with pytest.raises(OverlayRefused, match=built):
            asyncio.run(run())
        return
    sessions = asyncio.run(run())
    assert (sessions.login is not None, sessions.sealer is not None) == (built, built)
    assert sessions.signer is None, "a pool's sign-in is not a test sign-in page"
