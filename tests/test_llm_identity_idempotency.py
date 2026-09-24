"""The layer above config and telemetry: the model seam, identity, and the
ledger that decides whether an effect happens once or twice."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from evals import issuer as issuing

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import (
    IdempotencyKey,
    Identity,
    LLMClient,
    Message,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    RunId,
    Usage,
)
from support_agent.llm import ScriptedClient, UnavailableClient

ISSUER = issuing.issuer()
RUN = RunId("run_t")


def key(step: int = 1, iteration: int = 0) -> IdempotencyKey:
    return IdempotencyKey(run_id=RUN, step=step, iteration=iteration)


def request(text: str = "hello") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),))


# --------------------------------------------------------------------------- #
# The seam. Adapters satisfy LLMClient structurally — no adapter imports it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "client"),
    [
        ("scripted", ScriptedClient([])),
        ("unavailable", UnavailableClient()),
    ],
    ids=["scripted", "unavailable"],
)
@pytest.mark.discharges("AHC-0022")
def test_adapters_satisfy_the_protocol_structurally(name: str, client: object) -> None:
    assert isinstance(client, LLMClient)


@pytest.mark.discharges("AAC-0059", "AHC-0022")
async def test_scripted_client_is_deterministic_and_free() -> None:
    tel.configure()
    scripted = ScriptedClient([ModelResponse(text="one"), ModelResponse(text="two")])
    assert (await scripted.complete(request())).text == "one"
    assert (await scripted.complete(request())).text == "two"
    assert scripted.exhausted


@pytest.mark.tooling
async def test_exhausting_the_script_is_an_error_not_an_empty_reply() -> None:
    """A scenario that runs longer than its script did not test what it claimed."""
    tel.configure()
    scripted = ScriptedClient([ModelResponse(text="only")])
    await scripted.complete(request())
    with pytest.raises(ModelUnavailable, match="exhausted"):
        await scripted.complete(request())


@pytest.mark.discharges("AAC-0009")
async def test_provider_failure_is_typed_not_a_stack_trace() -> None:
    """AAC-0009 — degradation is a declared path."""
    with pytest.raises(ModelUnavailable):
        await UnavailableClient().complete(request())


@pytest.mark.discharges("AAC-0011", "B8")
async def test_model_call_emits_a_span_with_usage() -> None:
    exporter = tel.configure()
    scripted = ScriptedClient(
        [ModelResponse(text="hi", usage=Usage(input_tokens=10, output_tokens=4))]
    )
    await scripted.complete(request())
    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert attrs[tel.GEN_AI_INPUT_TOKENS] == 10
    assert attrs[tel.GEN_AI_OUTPUT_TOKENS] == 4


# --------------------------------------------------------------------------- #
# Identity. Authorisation comes from the token, never from anything said.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0099")
def test_round_trip_yields_the_customer_and_scopes() -> None:
    who = ident.verify(issuing.mint("C-1042"), issuer=ISSUER)
    assert who.customer_id == "C-1042"
    assert who.may(ident.SCOPE_ORDERS_READ)
    assert who.session, "jti is required, so every session can be named in the record"


def _other_key_token() -> str:
    """Signed by a key the agent was never given, with every claim right."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(issuing.mint("C-1042"), options={"verify_signature": False})
    return jwt.encode(claims, other, algorithm="RS256", headers={"kid": issuing.KID})


def _hs256_with_the_public_key() -> str:
    """Algorithm confusion: the issuer's *public* key, as PEM, used as an HMAC
    secret. A verifier that let the header choose the algorithm would accept it.
    Built by hand, because PyJWT refuses to sign it, which is its own defence
    and not ours."""
    claims = jwt.decode(issuing.mint("C-1042"), options={"verify_signature": False})
    pem = (
        issuing._private_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )

    def part(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = f"{part({'alg': 'HS256', 'typ': 'JWT', 'kid': issuing.KID})}.{part(claims)}"
    mac = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def _unsigned() -> str:
    claims = jwt.decode(issuing.mint("C-1042"), options={"verify_signature": False})
    return jwt.encode(claims, None, algorithm="none")


def _without_jti() -> str:
    claims = jwt.decode(issuing.mint("C-1042"), options={"verify_signature": False})
    claims.pop("jti")
    return jwt.encode(
        claims, issuing._private_key(), algorithm="RS256", headers={"kid": issuing.KID}
    )


# (why, how the token is made) — every one must produce no principal at all
REJECTION_CASES = [
    ("tampered payload", lambda: issuing.mint("C-1042")[:-4] + "AAAA"),
    ("not a token at all", lambda: "garbage"),
    ("signed by a key the agent was never given", _other_key_token),
    ("addressed to another service", lambda: issuing.mint("C-1042", audience="the-store-api")),
    ("issued by someone else", lambda: issuing.mint("C-1042", iss="https://elsewhere.test/")),
    ("HS256, keyed with the issuer's public key", _hs256_with_the_public_key),
    ("alg none", _unsigned),
    ("no jti", _without_jti),
]


@pytest.mark.discharges("AHC-0099", "AAC-0111")
@pytest.mark.parametrize(("name", "make"), REJECTION_CASES, ids=[c[0] for c in REJECTION_CASES])
def test_bad_tokens_produce_no_identity_at_all(name: str, make) -> None:
    with pytest.raises(ident.InvalidSession):
        ident.verify(make(), issuer=ISSUER)


@pytest.mark.discharges("AHC-0099", "AAC-0111")
def test_expired_token_is_refused() -> None:
    token = issuing.mint("C-1042", ttl_s=60, now=1_000_000)
    with pytest.raises(ident.InvalidSession):
        ident.verify(token, issuer=ISSUER, now=1_000_061)


# (why, the session's customer claim, whether it may act as a customer)
CUSTOMER_CASES = [
    ("a customer's session", "C-1042", True),
    ("a staff login has no customer, and is not given one", None, False),
    ("an empty customer claim is no customer", "", False),
]


@pytest.mark.discharges("AHC-0099", "AAC-0111", "P-OWNERSHIP")
@pytest.mark.parametrize(
    ("name", "customer", "ok"), CUSTOMER_CASES, ids=[c[0] for c in CUSTOMER_CASES]
)
def test_only_a_session_with_a_customer_acts_as_one(name: str, customer, ok: bool) -> None:
    """T-002 decision 1: the customer is its own claim, never `sub`. A fallback
    to `sub` would make every staff login a customer whose id is a UUID."""
    principal = ident.verify(issuing.mint(customer, subject="login-7"), issuer=ISSUER)
    if ok:
        who = principal.as_customer()
        assert (who.customer_id, who.subject, who.session) == (
            customer,
            "login-7",
            principal.session,
        )
    else:
        with pytest.raises(ident.NotACustomer):
            principal.as_customer()


@pytest.mark.discharges("AAC-0057", "AHC-0057")
def test_customer_scopes_exclude_refunds() -> None:
    """A refund is irreversible and needs a human above the threshold, so the
    agent acting as the customer must not hold the scope that would skip it."""
    assert ident.SCOPE_REFUNDS_WRITE not in ident.CUSTOMER_SCOPES
    who = ident.verify(issuing.mint("C-1"), issuer=ISSUER).as_customer()
    with pytest.raises(ident.InvalidSession, match="lacks scope"):
        ident.require(who, ident.SCOPE_REFUNDS_WRITE)


@pytest.mark.discharges("AHC-0099", "AAC-0111", "AHC-0034")
def test_confused_deputy_needs_a_different_token_not_a_different_claim() -> None:
    """T-AD-01. Whatever the model believes about who it is talking to, the
    identity it can act as is the one the token carries."""
    who = ident.verify(issuing.mint("C-1042"), issuer=ISSUER).as_customer()
    assert who.customer_id == "C-1042"
    assert Identity(customer_id="C-9999").customer_id != who.customer_id


# --------------------------------------------------------------------------- #
# Idempotency moved out — T-062.
#
# It lived here as `idempotency.once(ledger, key, side_effect, action)`, beside
# the identity a call carries, because the key is minted from the run and the
# call is made as somebody. The store is now one thing at two scopes and its
# rule is one sentence, so the tests are `tests/test_requests.py`: the same six
# cases, plus the delivery scope that used to be a second store answering the
# same question differently. What stays here is the identity half.
# --------------------------------------------------------------------------- #
