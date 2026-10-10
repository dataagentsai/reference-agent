"""Entra ID's browser sign-in (claims-fnol-azure A2): code flow, PKCE, nonce.

Offline: a locally generated RSA key signs tokens shaped as Entra's v2.0 ones,
and `httpx.MockTransport` plays the tenant's token endpoint, which checks the
PKCE verifier against the challenge the authorize URL carried, as Entra does.
What only a live tenant can show (a real registration's tokens, the
`extn.` claim, Entra's own PKCE check) is not here.
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

from agent_harness import adapters
from agent_harness.adapters import OverlayRefused, Wiring
from agent_harness.adapters.identity import ENTRA_ID
from agent_harness.identity import JWKS, InvalidSession
from agent_harness.identity.entra import entra_issuer, verify_entra
from agent_harness.identity.entra_login import EntraLogin, Flow, Sealer, Tokens

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"
REDIRECT = "https://agent.test/signin/callback"
KID = "entra-login-key"
HOLDER = "extn.customer_id"


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
    base = {"iss": ISS, "aud": CLIENT, "sub": "s-rohan", "oid": "o-rohan", "uti": uuid.uuid4().hex}
    return _signed(**{**base, "iat": now, "exp": now + 3600, HOLDER: "PH-1001", **over})


def _id_token(nonce: str, **over: Any) -> str:
    now = int(time.time())
    base = {"iss": ISS, "aud": CLIENT, "sub": "s-rohan", "oid": "o-rohan", "nonce": nonce}
    return _signed(**{**base, "iat": now, "exp": now + 3600, **over})


class FakeTokenEndpoint:
    """The tenant's `/token`: a code is good once, for the challenge it was issued with."""

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
        if form.get("client_secret") != "s3cret":
            return httpx.Response(401, json={"error": "invalid_client"})
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


def _login(endpoint: FakeTokenEndpoint) -> EntraLogin:
    return EntraLogin(
        TENANT,
        issuer=entra_issuer(TENANT, CLIENT, keys=_keys()),
        client_id=CLIENT,
        client_secret="s3cret",
        redirect_uri=REDIRECT,
        scopes=("api://claims-fnol/claims:read",),
        transport=httpx.MockTransport(endpoint.handle),
    )


def _verify(token: str) -> Any:
    issuer = entra_issuer(TENANT, CLIENT, keys=_keys())
    return verify_entra(token, issuer=issuer, holder_claim=HOLDER)


@pytest.mark.discharges("AAC-0057", "AHC-0099")
def test_the_authorize_url_carries_state_nonce_and_an_s256_challenge() -> None:
    flow = Flow()
    url = urlsplit(_login(FakeTokenEndpoint()).authorize_url(flow))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "login.microsoftonline.com"
    assert url.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert query["state"] == flow.state and query["nonce"] == flow.nonce
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == flow.challenge != flow.verifier
    assert query["scope"].split() == [
        "openid",
        "profile",
        "offline_access",
        "api://claims-fnol/claims:read",
    ]
    assert query["redirect_uri"] == REDIRECT and "client_secret" not in query


SIGN_IN = [
    # (row, what changes, the holder signed in, or the refusal's words)
    ("a good code exchange", {}, "PH-1001"),
    ("the PKCE verifier is not this flow's", {"verifier": "someone-elses"}, "invalid_grant"),
    ("the id token's nonce is not this flow's", {"nonce": "replayed"}, "nonce"),
    ("the access token is for another audience", {"aud": "another-app"}, "(?i)audience"),
    ("the id token names another user", {"id_oid": "o-meera"}, "different users"),
    ("no id token came back", {"no_id": True}, "no id token"),
    ("a code used twice", {"twice": True}, "invalid_grant"),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "change", "outcome"), SIGN_IN, ids=[r[0] for r in SIGN_IN])
def test_a_sign_in(row: str, change: dict[str, Any], outcome: str) -> None:
    endpoint = FakeTokenEndpoint({"aud": change["aud"]} if "aud" in change else None)
    login = _login(endpoint)
    flow = Flow()
    code = endpoint.authorize(login.authorize_url(flow))

    async def run() -> Any:
        sent = Flow(flow.state, change.get("verifier", flow.verifier), flow.nonce)
        if change.get("twice"):
            await login.redeem(code, sent)
        tokens = await login.redeem(code, sent)
        if "id_oid" in change or change.get("no_id"):
            other = None if change.get("no_id") else _id_token(flow.nonce, oid=change["id_oid"])
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
    # (row, the stored refresh token, the access token's holder or the refusal)
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
        assert keep == stored, "no new refresh token came back, so the old one is kept"
    else:
        with pytest.raises(InvalidSession, match=outcome):
            asyncio.run(login.refresh(stored))


NOW = [1_800_000_000.0]
SEALS = [
    # (row, how the sealed value is handled, opens)
    ("sealed and opened in time", lambda v, s: s.open(v, 600), True),
    ("opened after it expired", lambda v, s: (NOW.append(NOW[0] + 601), s.open(v, 600))[1], False),
    ("tampered with", lambda v, s: s.open(v[:-4] + "AAAA", 600), False),
    ("sealed under another key", lambda v, s: Sealer(b"B" * 43 + b"=").open(v, 600), False),
    ("absent", lambda v, s: s.open(None, 600), False),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "handle", "opens"), SEALS, ids=[r[0] for r in SEALS])
def test_a_sealed_cookie(row: str, handle: Any, opens: bool) -> None:
    NOW[:] = [1_800_000_000.0]
    sealer = Sealer(adapters.sealing_key("secret", "test"), clock=lambda: NOW[-1])
    value = sealer.seal({"sub": "s-rohan"})
    assert "s-rohan" not in value, "the browser cannot read what it carries"
    assert (handle(value, sealer) == {"sub": "s-rohan"}) is opens


@pytest.mark.discharges("AAC-0057", "AHC-0099")
def test_one_secret_gives_unrelated_keys_per_purpose() -> None:
    assert adapters.sealing_key("x", "cookies") != adapters.sealing_key("x", "sessions")
    assert len(base64.urlsafe_b64decode(adapters.sealing_key("x", "cookies"))) == 32


ADAPTER = [
    # (row, settings beyond the tenant and audience, a sign-in built or the refusal)
    ("no redirect URI: no sign-in", {}, False),
    (
        "a redirect URI with the client and a key: the sign-in",
        {"client_id": CLIENT, "client_secret": "s3cret", "session_key": "k"},
        True,
    ),
    ("a redirect URI without a key is refused", {"client_id": CLIENT, "client_secret": "s"}, None),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(("row", "extra", "built"), ADAPTER, ids=[r[0] for r in ADAPTER])
def test_the_entra_adapter_builds_the_sign_in(row: str, extra: dict[str, Any], built: Any) -> None:
    settings = {
        "tenant": TENANT,
        "audience": CLIENT,
        "holder_claim": HOLDER,
        "far_end_scope": "api://claims-system/.default",
        "login_redirect_uri": REDIRECT if extra else None,
        **extra,
    }

    async def run() -> Any:
        wiring = Wiring(settings, {}, {"keys": _keys()}, "test")
        async with ENTRA_ID.build(wiring) as sessions:
            return sessions

    if built is None:
        with pytest.raises(OverlayRefused, match="session_key"):
            asyncio.run(run())
        return
    sessions = asyncio.run(run())
    assert (sessions.login is not None) is built and (sessions.sealer is not None) is built
    assert sessions.verify(_access()).customer_id == "PH-1001", "the holder claim is the setting"
