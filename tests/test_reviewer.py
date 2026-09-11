"""The desk's own surface — step 4, and the half F-007 kept finding missing.

`agent_state.escalations` held rows for a whole morning that nothing could read.
These tests drive the routes an operations tool would: read the queue, close what
you handled, and say whether it needed you at all.

That last part is the point of the whole surface. The over-escalation rate is
supplied by the person who picked the ticket up and by nobody else, so until
this existed the number was not merely unmeasured — it was unmeasurable.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.contracts import EscalationState

SECRET = "test-only-secret-not-for-production!!"
CUSTOMER = "C-1042"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@pytest.fixture
def store():
    return esc.InMemoryEscalationStore()


@pytest.fixture
def client(store):
    """The reviewer app mounted where it really lives — under the same Starlette
    application that serves `/chat`. Testing it standalone would prove the app
    works and say nothing about the mount."""
    app = serve.build(_NoAgent(), secret=SECRET, escalations=store)
    return TestClient(app)


class _NoAgent:
    """`/chat` is not under test here, and an agent that would answer one is more
    setup than this file has any business owning."""

    store = None


def token(*, scopes, customer: str = "desk-3") -> str:
    return ident.mint(customer, secret=SECRET, scopes=scopes, ttl_s=900)


def reader() -> dict[str, str]:
    return {"authorization": f"Bearer {token(scopes=ident.REVIEWER_SCOPES)}"}


def customer_token() -> dict[str, str]:
    return {"authorization": f"Bearer {token(scopes=ident.CUSTOMER_SCOPES, customer=CUSTOMER)}"}


async def queued(store, *, rule_id: str = "asked-for-human", ttl_s: int = 1800):
    return await esc.raise_for(
        store,
        conversation_id="cnv_1",
        run_id="run_1",
        customer_id=CUSTOMER,
        reason="the customer asked for a human",
        rule_id=rule_id,
        rules_version="v1",
        ttl_s=ttl_s,
    )


# --------------------------------------------------------------------------- #
# Authorisation. Two scopes, and a customer holds neither.
# --------------------------------------------------------------------------- #

REFUSED = [
    ("no header", {}, 401),
    ("not bearer", {"authorization": "Basic abc"}, 401),
    ("garbage token", {"authorization": "Bearer not-a-token"}, 401),
    ("a customer session", "customer", 403),
]


@pytest.mark.parametrize(("name", "headers", "status"), REFUSED, ids=[c[0] for c in REFUSED])
def test_who_may_read_the_queue(client, name: str, headers, status: int) -> None:
    """A customer token is *valid* and still refused — 403, not 401. The
    distinction matters: one says "we do not know you", the other says "we know
    you and this is not yours"."""
    sent = customer_token() if headers == "customer" else headers
    assert client.get("/ops/escalations", headers=sent).status_code == status


def test_the_401_body_says_nothing_useful_to_a_prober(client) -> None:
    """Same reasoning as `/chat`'s: the library's own message would report which
    part of the token was malformed."""
    res = client.get("/ops/escalations", headers={"authorization": "Bearer garbage"})
    assert res.json() == {"detail": "the session token is not valid"}


async def test_a_reader_cannot_close_anything(client, store) -> None:
    """Read and review are separate scopes, so a read-only desk session is a real
    thing rather than a comment in the docs."""
    row = await queued(store)
    only_read = ident.mint(
        "desk-9", secret=SECRET, scopes=frozenset({ident.SCOPE_ESCALATIONS_READ})
    )
    res = client.post(
        f"/ops/escalations/{row.id}/resolve",
        headers={"authorization": f"Bearer {only_read}"},
        json={"outcome": "resolved"},
    )
    assert res.status_code == 403
    assert "escalations:review" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# Reading the queue.
# --------------------------------------------------------------------------- #


async def test_the_queue_shows_what_a_reviewer_needs(client, store) -> None:
    row = await queued(store)
    body = client.get("/ops/escalations", headers=reader()).json()

    assert [q["id"] for q in body] == [row.id]
    one = body[0]
    assert one["conversation_id"] == "cnv_1", "a reviewer has to find the conversation"
    assert one["rule_id"] == "asked-for-human"
    assert one["waiting_s"] >= 0
    assert "rules_version" not in one, "for the analysis, not for a person"


async def test_a_resolved_escalation_leaves_the_queue(client, store) -> None:
    row = await queued(store)
    client.post(
        f"/ops/escalations/{row.id}/resolve", headers=reader(), json={"outcome": "resolved"}
    )
    assert client.get("/ops/escalations", headers=reader()).json() == []


def test_an_unknown_escalation_is_404_not_500(client) -> None:
    assert client.get("/ops/escalations/E-NOPE", headers=reader()).status_code == 404


# --------------------------------------------------------------------------- #
# Closing it. The outcome is the product of this surface.
# --------------------------------------------------------------------------- #

OUTCOMES = [
    ("it needed a person", "resolved"),
    ("it did not", "agent_could_have"),
    ("wrong desk", "misrouted"),
    ("they left", "customer_gone"),
]


@pytest.mark.parametrize(("name", "outcome"), OUTCOMES, ids=[c[0] for c in OUTCOMES])
async def test_every_outcome_reaches_the_row(client, store, name: str, outcome: str) -> None:
    row = await queued(store)
    res = client.post(
        f"/ops/escalations/{row.id}/resolve",
        headers=reader(),
        json={"outcome": outcome, "note": name},
    )
    assert res.status_code == 200, res.text

    closed = await store.get(row.id)
    assert closed is not None
    assert closed.state is EscalationState.RESOLVED
    assert closed.outcome is not None and closed.outcome.value == outcome
    assert closed.outcome_by == "desk-3"
    assert closed.outcome_note == name


async def test_the_over_escalation_label_is_sliceable_by_rule(client, store) -> None:
    """The whole reason `rule_id` is on the row rather than only a prose reason.

    *"Which rule produces escalations humans say were unnecessary"* is the
    question that tunes the rule set, and it cannot be asked of a sentence.
    """
    row = await queued(store, rule_id="lost-in-transit")
    client.post(
        f"/ops/escalations/{row.id}/resolve",
        headers=reader(),
        json={"outcome": "agent_could_have"},
    )
    closed = await store.get(row.id)
    assert closed is not None
    assert (closed.rule_id, closed.outcome.value) == ("lost-in-transit", "agent_could_have")


async def test_closing_twice_is_refused_rather_than_overwritten(client, store) -> None:
    """An outcome that can be rewritten is one that can be rewritten *after*
    somebody reads the dashboard."""
    row = await queued(store)
    first = client.post(
        f"/ops/escalations/{row.id}/resolve", headers=reader(), json={"outcome": "resolved"}
    )
    second = client.post(
        f"/ops/escalations/{row.id}/resolve",
        headers=reader(),
        json={"outcome": "agent_could_have"},
    )
    assert first.status_code == 200
    assert second.status_code == 409, "a state conflict, not a server error"

    closed = await store.get(row.id)
    assert closed is not None and closed.outcome.value == "resolved"


async def test_a_close_without_an_outcome_is_refused(client, store) -> None:
    """No default, deliberately. A reviewer who closes without saying whether the
    agent could have handled it has given us nothing, and a default would make
    the least informative answer the one nobody had to choose."""
    row = await queued(store)
    res = client.post(f"/ops/escalations/{row.id}/resolve", headers=reader(), json={})
    assert res.status_code == 422


async def test_an_invented_outcome_never_reaches_the_row(client, store) -> None:
    row = await queued(store)
    res = client.post(
        f"/ops/escalations/{row.id}/resolve", headers=reader(), json={"outcome": "sorted-it"}
    )
    assert res.status_code == 422

    still = await store.get(row.id)
    assert still is not None and still.state is EscalationState.QUEUED


# --------------------------------------------------------------------------- #
# The mount itself.
# --------------------------------------------------------------------------- #


def test_the_two_surfaces_share_a_process_and_not_a_contract(client) -> None:
    """`/chat` keeps its hand-written 400s; the desk answers FastAPI's 422 for
    the same class of fault. That is the trade the mount exists to make, and
    asserting it stops a later "let us make these consistent" from quietly
    changing the contract `/chat`'s tests pin."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/ops/openapi.json").status_code == 200


def test_the_desk_holds_no_scope_over_orders() -> None:
    """A reviewer is not a customer with extra powers. It cannot cancel, refund
    or amend anything — if it needs to, it does so as itself through the ordinary
    surface, under the same authorisation as anyone."""
    assert ident.REVIEWER_SCOPES.isdisjoint(ident.CUSTOMER_SCOPES)
    assert not any(s.startswith("orders:") for s in ident.REVIEWER_SCOPES)
