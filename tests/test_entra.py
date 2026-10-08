"""Entra ID as the issuer (T-099): its sessions verified, and exchanged on behalf of.

Offline throughout: a locally generated RSA key signs tokens shaped as Entra's
v2.0 access tokens, and `httpx.MockTransport` plays the tenant's token endpoint.
What only a live tenant can show — that a real app registration's tokens carry
these claims, and that the far end accepts the exchanged token — is not here.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import cache
from typing import Any
from urllib.parse import parse_qs

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_harness.contracts import Identity
from agent_harness.contracts.failures import Fault
from agent_harness.identity import JWKS, InvalidSession, RemoteJWKS
from agent_harness.identity.entra import (
    EntraMalformed,
    EntraMisconfigured,
    EntraOnBehalfOf,
    EntraUnreachable,
    entra_issuer,
    verify_entra,
)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AUDIENCE = f"api://{CLIENT_ID}"
ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KID = "entra-test-key"
NOW = 1_800_000_000


@cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, Any]:
    public = RSAAlgorithm.to_jwk(_key().public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]}


def _token(drop: tuple[str, ...] = (), *, alg: str = "RS256", **over: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISS,
        "aud": AUDIENCE,
        "sub": "login-c1042",
        "iat": NOW - 60,
        "exp": NOW + 3600,
        "uti": "uti-1",
        "azp": "portal-client",
        "scp": "orders.read orders.write",
        "customer_id": "C-1042",
        "tid": TENANT,
        **over,
    }
    for name in drop:
        claims.pop(name)
    if alg == "HS256":
        return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": KID})
    return jwt.encode(claims, _key(), algorithm=alg, headers={"kid": KID})


def _issuer() -> Any:
    return entra_issuer(TENANT, AUDIENCE, keys=JWKS(_jwks()))


# ---------------------------------------------------------------- configuration


def test_the_issuer_is_the_tenants_v2_endpoint_and_key_set() -> None:
    issuer = entra_issuer(TENANT, CLIENT_ID)
    assert issuer.url == ISS
    assert issuer.audience == CLIENT_ID
    assert isinstance(issuer.keys, RemoteJWKS)
    # PyJWKClient keeps the URI it will fetch; constructing it fetches nothing.
    assert issuer.keys._client.uri == (  # noqa: SLF001
        f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    )


# ---------------------------------------------------------------- verification

VERIFY = [
    # (id, token, expected principal fields or None for refused)
    (
        "delegated scopes from a space-separated scp",
        lambda: _token(),
        {"customer_id": "C-1042", "scopes": {"orders.read", "orders.write"}, "session": "uti-1"},
    ),
    (
        "an app's own token: roles, no scp, no customer",
        lambda: _token(("scp", "customer_id"), roles=["approvals.decide"], sub="mi-oid"),
        {"customer_id": None, "scopes": {"approvals.decide"}, "session": "uti-1"},
    ),
    (
        "scp that is not a string grants nothing",
        lambda: _token(scp=["orders.read"]),
        {"customer_id": "C-1042", "scopes": set(), "session": "uti-1"},
    ),
    ("expired", lambda: _token(exp=NOW), None),
    ("another tenant's issuer", lambda: _token(iss=ISS.replace("1111", "9999")), None),
    ("another app's audience", lambda: _token(aud="api://someone-else"), None),
    ("no uti to name the session", lambda: _token(("uti",)), None),
    ("HS256 is not on the allow-list", lambda: _token(alg="HS256"), None),
    ("tampered payload", lambda: _token()[:-4] + "AAAA", None),
]


@pytest.mark.parametrize(("case", "make", "expected"), VERIFY, ids=[v[0] for v in VERIFY])
@pytest.mark.discharges("AAC-0057", "AHC-0099")
def test_verify_entra(case: str, make: Any, expected: dict[str, Any] | None) -> None:
    token = make()
    if expected is None:
        with pytest.raises(InvalidSession):
            verify_entra(token, issuer=_issuer(), now=NOW)
        return
    principal = verify_entra(token, issuer=_issuer(), now=NOW)
    assert principal.customer_id == expected["customer_id"], case
    assert set(principal.scopes) == expected["scopes"], case
    assert principal.session == expected["session"], case
    assert principal.token == token


# ---------------------------------------------------------------- on-behalf-of


def _identity(token: str = "customer-session") -> Identity:
    return Identity(
        customer_id="C-1042",
        scopes=frozenset({"orders.read"}),
        token=token,
        subject="login-c1042",
        session="uti-1",
    )


def _answering(status: int, body: Any = None, *, raise_exc: Exception | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if raise_exc is not None:
            raise raise_exc
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), seen


GRANTED = {"access_token": "far-end-token", "expires_in": 3600, "token_type": "Bearer"}

EXCHANGE = [
    # (id, status, body, transport exception, expected token or failure, fault)
    ("granted", 200, GRANTED, None, "far-end-token", None),
    ("expired session", 400, {"error": "invalid_grant"}, None, InvalidSession, Fault.REFUSED),
    ("no consent", 400, {"error": "consent_required"}, None, InvalidSession, Fault.REFUSED),
    ("MFA wanted", 400, {"error": "interaction_required"}, None, InvalidSession, Fault.REFUSED),
    ("unknown 4xx", 403, {"error": "access_denied"}, None, InvalidSession, Fault.REFUSED),
    (
        "wrong secret",
        401,
        {"error": "invalid_client"},
        None,
        EntraMisconfigured,
        Fault.MISCONFIGURED,
    ),
    ("bad scope", 400, {"error": "invalid_scope"}, None, EntraMisconfigured, Fault.MISCONFIGURED),
    ("throttled", 429, {"error": "temporarily_unavailable"}, None, EntraUnreachable, None),
    ("server error", 503, b"<html>down</html>", None, EntraUnreachable, Fault.UNREACHABLE),
    ("timeout", 0, None, httpx.ReadTimeout("slow"), EntraUnreachable, Fault.UNREACHABLE),
    ("connection refused", 0, None, httpx.ConnectError("no"), EntraUnreachable, None),
    ("200 without a token", 200, {"token_type": "Bearer"}, None, EntraMalformed, Fault.MALFORMED),
    ("200 not JSON", 200, b"not json", None, EntraMalformed, Fault.MALFORMED),
]


@pytest.mark.parametrize(
    ("case", "status", "body", "exc", "expected", "fault"), EXCHANGE, ids=[e[0] for e in EXCHANGE]
)
@pytest.mark.discharges("AHC-0099", "AHC-0110")
def test_on_behalf_of(
    case: str, status: int, body: Any, exc: Exception | None, expected: Any, fault: Fault | None
) -> None:
    transport, seen = _answering(status, body, raise_exc=exc)
    obo = EntraOnBehalfOf(
        TENANT,
        client_id=CLIENT_ID,
        scope="api://order-system/.default",
        client_secret="s3cret",
        transport=transport,
    )
    if isinstance(expected, str):
        assert asyncio.run(obo.for_far_end(_identity())) == expected, case
    else:
        with pytest.raises(expected) as raised:
            asyncio.run(obo.for_far_end(_identity()))
        if fault is not None:
            assert raised.value.fault is fault, case
        assert "s3cret" not in str(raised.value), "the secret never reaches a message"
    assert len(seen) == 1
    assert str(seen[0].url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


CREDENTIALS = [
    # (id, kwargs, fields that must be posted, fields that must not)
    (
        "client secret",
        {"client_secret": "s3cret"},
        {"client_secret": "s3cret"},
        {"client_assertion", "client_assertion_type"},
    ),
    (
        "client assertion (federated managed identity)",
        {"client_assertion": lambda: "signed.mi.jwt"},
        {
            "client_assertion": "signed.mi.jwt",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        },
        {"client_secret"},
    ),
]


@pytest.mark.parametrize(
    ("case", "kwargs", "present", "absent"), CREDENTIALS, ids=[c[0] for c in CREDENTIALS]
)
def test_the_grant_posted(
    case: str, kwargs: dict[str, Any], present: dict[str, str], absent: set[str]
) -> None:
    transport, seen = _answering(200, GRANTED)
    obo = EntraOnBehalfOf(
        TENANT,
        client_id=CLIENT_ID,
        scope="api://order-system/.default",
        transport=transport,
        **kwargs,
    )
    asyncio.run(obo.for_far_end(_identity("the-customers-token")))
    posted = {k: v[0] for k, v in parse_qs(seen[0].content.decode()).items()}
    expected = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "requested_token_use": "on_behalf_of",
        "assertion": "the-customers-token",
        "scope": "api://order-system/.default",
        "client_id": CLIENT_ID,
        **present,
    }
    assert {k: posted.get(k) for k in expected} == expected, case
    assert absent.isdisjoint(posted), case


CONSTRUCTION = [
    ("neither credential", {}),
    ("both credentials", {"client_secret": "s", "client_assertion": lambda: "a"}),
]


@pytest.mark.parametrize(("case", "kwargs"), CONSTRUCTION, ids=[c[0] for c in CONSTRUCTION])
def test_exactly_one_credential(case: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(EntraMisconfigured):
        EntraOnBehalfOf(TENANT, client_id=CLIENT_ID, scope="s", **kwargs)


CACHING = [
    # (id, seconds later, same session, exchanges expected)
    ("same session, a minute later: cached", 60, True, 1),
    ("same session, within 30s of expiry: fresh", 3600 - 20, True, 2),
    ("another session: its own exchange", 1, False, 2),
]


@pytest.mark.parametrize(("case", "later", "same", "calls"), CACHING, ids=[c[0] for c in CACHING])
def test_one_exchange_per_session(case: str, later: int, same: bool, calls: int) -> None:
    transport, seen = _answering(200, GRANTED)
    clock = [float(NOW)]
    obo = EntraOnBehalfOf(
        TENANT,
        client_id=CLIENT_ID,
        scope="s",
        client_secret="x",
        transport=transport,
        clock=lambda: clock[0],
    )

    async def twice() -> None:
        await obo.for_far_end(_identity("one"))
        clock[0] += later
        await obo.for_far_end(_identity("one" if same else "two"))

    asyncio.run(twice())
    assert len(seen) == calls, case


def test_no_session_is_refused_before_any_request() -> None:
    transport, seen = _answering(200, GRANTED)
    obo = EntraOnBehalfOf(
        TENANT, client_id=CLIENT_ID, scope="s", client_secret="x", transport=transport
    )
    with pytest.raises(InvalidSession):
        asyncio.run(obo.for_far_end(_identity("")))
    assert seen == []


def test_a_fresh_token_round_trips_through_verify() -> None:
    """What the far end would do with Entra's token, run here against our key:
    the same verifier, configured for the far end's audience."""
    far_end = entra_issuer(TENANT, "api://order-system", keys=JWKS(_jwks()))
    token = _token(aud="api://order-system", azp=CLIENT_ID, uti=uuid.uuid4().hex)
    principal = verify_entra(token, issuer=far_end, now=NOW)
    assert principal.party == CLIENT_ID
    assert principal.customer_id == "C-1042"
