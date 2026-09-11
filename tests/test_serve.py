"""The edge — P1, which R-017 found was empty.

Every argument `handle` takes has to come from somewhere, and each one is a
decision with a wrong answer that looks fine until it is exploited. These tests
are mostly about the wrong answers.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from agenttwin import Live, load, project
from starlette.testclient import TestClient

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.contracts import ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
SECRET = "test-secret-long-enough-to-be-allowed-32"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def token(customer: str = "C-1042", **kw) -> str:
    return ident.mint(customer, secret=SECRET, now=int(time.time()), **kw)


@pytest.fixture
def client():
    """A real Starlette app over a real projected shop, nothing stubbed but the
    model.

    The agent is built by a **factory** rather than passed in ready, so its MCP
    connection is opened and closed inside the app's own lifespan — the same
    task, which is what anyio requires and what a deployment would do anyway.
    """
    world = Live.start(load(WORLD))
    store = InMemoryCheckpointStore()

    @asynccontextmanager
    async def make_agent():
        async with connect(project(world), ledger=InMemoryLedger()) as tools:
            yield ep.build(
                llm=ScriptedClient([ModelResponse(text="Thanks — looking now.")] * 60),
                tools=tools,
                store=store,
                deliveries=trg.InMemoryDeliveryLog(),
            )

    with TestClient(serve.build(make_agent, secret=SECRET)) as c:
        c.store = store  # type: ignore[attr-defined]
        c.world = world  # type: ignore[attr-defined]
        yield c


def post(
    client,
    text: str,
    *,
    tok: str | None = None,
    cid: str | None = None,
    delivery: str | None = None,
    raw: dict | None = None,
):
    headers = {"content-type": "application/json"}
    if tok is not None:
        headers["authorization"] = f"Bearer {tok}"
    if delivery:
        headers["idempotency-key"] = delivery
    body = raw if raw is not None else {"text": text}
    if cid:
        body["conversation_id"] = cid
    return client.post("/chat", json=body, headers=headers)


# --------------------------------------------------------------------------- #
# Who is asking.
# --------------------------------------------------------------------------- #


def test_a_turn_needs_a_token(client) -> None:
    assert post(client, "hello").status_code == 401


def test_a_forged_token_is_refused_without_saying_why(client) -> None:
    """The library's own message describes what was wrong with the token —
    which segment, which codec — and every word of that helps somebody
    guessing. The detail goes on the span instead."""
    res = post(client, "hello", tok="not.a.real.token")

    assert res.status_code == 401
    assert res.json() == {"error": "the session token is not valid"}
    body = res.text.lower()
    for leak in ("codec", "utf-8", "padding", "signature", "traceback", "jwt"):
        assert leak not in body, f"the refusal leaked {leak!r}"


def test_an_expired_token_is_refused(client) -> None:
    stale = ident.mint("C-1042", secret=SECRET, ttl_s=1, now=int(time.time()) - 100)
    assert post(client, "hello", tok=stale).status_code == 401


def test_identity_comes_from_the_token_not_the_body(client) -> None:
    """The single most important line at this layer.

    If `customer_id` were read from the request, every customer could name
    themselves any other customer and every scope check below would be
    enforcing a claim the attacker wrote.
    """
    res = post(
        client,
        "hello",
        tok=token("C-1042"),
        raw={"text": "hello", "customer_id": "C-9999", "scopes": ["refunds:write"]},
    )

    assert res.status_code == 200
    cid = res.json()["conversation_id"]
    raw = client.store._conversations[cid]  # type: ignore[attr-defined]
    assert b'"customer_id":"C-1042"' in raw, "the body's claim was believed"


# --------------------------------------------------------------------------- #
# What they said.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "body", "status"),
    [
        ("no text", {}, 400),
        ("empty text", {"text": ""}, 400),
        ("whitespace only", {"text": "   "}, 400),
        ("text is not a string", {"text": 42}, 400),
        ("body is not an object", [1, 2, 3], 400),
        ("conversation id is not a string", {"text": "hi", "conversation_id": 7}, 400),
    ],
)
@pytest.mark.discharges("AHC-0016", "B10", "AAC-0015")
def test_a_malformed_request_is_refused(client, why: str, body, status: int) -> None:
    res = client.post("/chat", json=body, headers={"authorization": f"Bearer {token()}"})
    assert res.status_code == status, why


@pytest.mark.discharges("AHC-0016", "B10", "AAC-0015")
def test_an_oversized_message_is_refused_before_it_is_parsed(client) -> None:
    """A support message is a sentence. Anything larger is a mistake or an
    attack, and both are cheaper to refuse than to parse."""
    res = post(client, "x" * (serve.MAX_BODY + 100), tok=token())
    assert res.status_code == 413


# --------------------------------------------------------------------------- #
# Which conversation — F-006, over HTTP.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0066")
def test_a_conversation_continues_across_requests(client) -> None:
    """Impossible before F-006 was fixed. Checkpoints were filed under run id, a
    fresh one is minted every turn, and no caller has ever held one — so there
    was no way to turn what a customer presents into state."""
    first = post(client, "hello", tok=token()).json()
    cid = first["conversation_id"]

    second = post(client, "and my order AB-10004?", tok=token(), cid=cid).json()
    assert second["conversation_id"] == cid

    raw = client.store._conversations[cid]  # type: ignore[attr-defined]
    assert b"and my order AB-10004?" in raw
    assert b"hello" in raw, "the earlier turn survived"


@pytest.mark.discharges("AAC-0040")
def test_another_customer_cannot_open_your_conversation(client) -> None:
    """Refused as *not found* rather than *forbidden* — confirming it exists
    tells an attacker their guess was right."""
    cid = post(client, "hello", tok=token("C-1042")).json()["conversation_id"]

    res = post(client, "what did I say?", tok=token("C-9999"), cid=cid)
    assert res.status_code == 404
    assert "forbidden" not in res.text.lower()


@pytest.mark.discharges("AHC-0066")
def test_an_unknown_conversation_id_starts_a_new_one(client) -> None:
    res = post(client, "hello", tok=token(), cid="cnv_nothing_here")
    assert res.status_code == 200
    assert res.json()["conversation_id"] != "cnv_nothing_here"


# --------------------------------------------------------------------------- #
# Which delivery — AAC-0076, over HTTP.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0076", "AHC-0053")
def test_a_repeated_delivery_is_answered_not_repeated(client) -> None:
    """200, not an error. The caller did the right thing by retrying, and a 4xx
    here would make every well-behaved queue look like a client fault."""
    first = post(client, "cancel AB-10002", tok=token(), delivery="msg-1")
    second = post(client, "cancel AB-10002", tok=token(), delivery="msg-1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already handled"


def test_without_an_idempotency_key_the_turn_runs_unguarded(client) -> None:
    """Stated rather than defaulted. Inventing an id here would produce a guard
    that can never fire, and a green result to go with it."""
    a = post(client, "hello", tok=token())
    b = post(client, "hello", tok=token())
    assert a.status_code == b.status_code == 200
    assert a.json()["conversation_id"] != b.json()["conversation_id"]


# --------------------------------------------------------------------------- #
# What comes back.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("R-DISCOUNT", "AHC-0017")
def test_a_refusal_is_a_two_hundred(client) -> None:
    """The agent worked correctly and the answer is no. A 4xx would make every
    dashboard count correct behaviour as an error rate."""
    res = post(client, "can I get a discount?", tok=token())

    assert res.status_code == 200
    assert res.json()["outcome"] == "refused"


def test_health_does_not_touch_the_model(client) -> None:
    """A health check that calls the provider fails when the provider is slow,
    and an orchestrator then restarts a process that was working."""
    assert client.get("/healthz").json() == {"status": "ok"}


def test_the_page_is_served_and_cannot_mint_its_own_identity(client) -> None:
    """A page that could sign its own token would make every scope check below
    it decorative.

    Asserted on what the page *can do*, not on words it contains — an earlier
    version of this test searched for the string "mint" and failed on a comment
    explaining that the browser mints a delivery id, which is a different thing
    and perfectly fine.
    """
    page = client.get("/").text

    assert "<form" in page and "idempotency-key" in page, "it is a chat page"
    assert SECRET not in page, "the signing secret reached the browser"
    for signing in ("HS256", "hmac", "createSign", "jsonwebtoken", "crypto.subtle.sign"):
        assert signing not in page, f"the page can sign tokens itself ({signing})"
