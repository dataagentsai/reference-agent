"""The approvals desk over HTTP: read what is waiting, and decide it (T-059).

Against real approval workflows on the test server, mounted where the desk
really lives — under the application that serves `/chat`. What each row is
about is **authority**: who may read, who may decide, and which refusals the
workflow makes reach the person as a fact they can act on rather than as a
fault.

Until this existed an approval could only be decided from code, so every one
expired unattended.

The desk is opened by a context manager inside each test rather than by a
fixture: deciding talks to Temporal, and that connection has to be entered and
left in one task — the same reason `test_approver` is written this way.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
import pytest
from agenttwin import Live, load, project
from evals import durable
from evals import issuer as issuing
from tests.test_watching import WORLD

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.contracts import ApprovalState, IdempotencyKey, Identity, RunId
from support_agent.requests import InMemoryRequests
from support_agent.tools import connect

ISSUER = issuing.issuer()
CUSTOMER = "C-1042"
ORDER = "AB-10003"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


async def acting_for(customer_id: str) -> Identity:
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


class _NoAgent:
    """`/chat` is not under test here, and an agent that would answer one is
    more setup than this file has any business owning."""

    store = None


@asynccontextmanager
async def desk_open(*, wired: bool = True):
    """The desk, mounted under the application that serves `/chat`."""
    world = Live.start(load(WORLD))
    async with (
        connect(project(world), requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools, acting_for=acting_for) as waits,
    ):
        app = serve.build(
            _NoAgent(),
            issuer=ISSUER,
            escalations=durable.RememberedEscalations(),
            approvals=waits.approvals if wired else None,
            approver=ap.ApprovalDesk(waits.env.client) if wired else None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://desk.test"
        ) as client:
            yield client, waits.approvals


def token(*, scopes, subject: str = "desk-3", customer: str | None = None) -> str:
    return issuing.mint(customer, scopes=scopes, subject=subject, ttl_s=900)


def approver() -> str:
    return token(scopes=ident.APPROVER_SCOPES)


def bearer(tok: str | None = None) -> dict[str, str]:
    return {"authorization": f"Bearer {tok or approver()}"}


async def waiting_refund(approvals: Any, *, order: str = ORDER) -> str:
    """A refund above the limit, raised as the agent raises one, left waiting."""
    key = IdempotencyKey(run_id=RunId(f"run_desk_{int(time.time() * 1000)}"), step=0, iteration=0)
    raised = await approvals.request(
        action=ap.REFUND_ACTION,
        args={"order_id": order},
        identity=Identity(customer_id=CUSTOMER, scopes=ident.CUSTOMER_SCOPES),
        idempotency_key=key,
    )
    assert raised.state is ApprovalState.WAITING, raised.state
    return str(raised.id)


# --------------------------------------------------------------------------- #
# Who may look.
# --------------------------------------------------------------------------- #

# (why, the scopes the caller holds, the status the queue answers)
READERS = [
    ("an approver reads the queue", ident.APPROVER_SCOPES, 200),
    ("an escalation reviewer holds no approvals scope", ident.REVIEWER_SCOPES, 403),
    ("a customer never reaches it", ident.CUSTOMER_SCOPES, 403),
    ("no scopes at all", frozenset(), 403),
]


@pytest.mark.parametrize(("why", "scopes", "status"), READERS, ids=[r[0] for r in READERS])
@pytest.mark.discharges("AAC-0057", "AHC-0057")
async def test_only_an_approver_may_read_what_is_waiting(
    why: str, scopes: frozenset[str], status: int
) -> None:
    async with desk_open() as (desk, approvals):
        await waiting_refund(approvals)
        answered = await desk.get("/ops/approvals", headers=bearer(token(scopes=scopes)))
    assert answered.status_code == status


@pytest.mark.discharges("AHC-0057")
async def test_an_unsigned_caller_is_refused_the_way_chat_refuses_one() -> None:
    async with desk_open() as (desk, _):
        assert (await desk.get("/ops/approvals")).status_code == 401
        answered = await desk.get("/ops/approvals", headers=bearer("nonsense"))
    assert answered.status_code == 401
    assert answered.json()["detail"] == "the session token is not valid"


@pytest.mark.discharges("AAC-0080", "P-APPROVAL-WAIT")
async def test_the_queue_shows_what_it_is_for_and_how_long_it_has_waited() -> None:
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals)
        rows = (await desk.get("/ops/approvals", headers=bearer())).json()

    assert [row["id"] for row in rows] == [approval_id]
    row = rows[0]
    assert row["action"] == ap.REFUND_ACTION
    # What it is for, in the row itself: an approver cannot decide a refund
    # without seeing which order it is and what it comes to.
    assert row["args"]["order_id"] == ORDER
    assert row["args"]["amount"], "the figure is the decision"
    assert row["customer_id"] == CUSTOMER
    assert row["state"] == ApprovalState.WAITING.value
    assert row["waiting_s"] >= 0
    assert row["expires_at"] > row["created_at"]


@pytest.mark.discharges("AAC-0080")
async def test_one_approval_reads_on_its_own_and_a_stranger_id_is_not_found() -> None:
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals)
        found = await desk.get(f"/ops/approvals/{approval_id}", headers=bearer())
        missing = await desk.get("/ops/approvals/apr_nothing", headers=bearer())

    assert found.json()["id"] == approval_id
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Deciding.
# --------------------------------------------------------------------------- #

# (why, what the person decided, the state that follows, whether anything was done)
DECISIONS = [
    ("granted, and the workflow carries it out", True, ApprovalState.DONE, True),
    ("refused, and nothing happens", False, ApprovalState.REFUSED, False),
]


@pytest.mark.parametrize(
    ("why", "granted", "state", "acted"), DECISIONS, ids=[r[0] for r in DECISIONS]
)
@pytest.mark.discharges("AAC-0078", "AHC-0057", "op:issue_refund")
async def test_a_decision_is_carried_out_by_the_workflow_not_by_the_desk(
    why: str, granted: bool, state: ApprovalState, acted: bool
) -> None:
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals)
        answered = await desk.post(
            f"/ops/approvals/{approval_id}/decide",
            json={"granted": granted, "note": "checked the order"},
            headers=bearer(),
        )

    assert answered.status_code == 200, answered.text
    decided = answered.json()
    assert (decided["state"], decided["granted"]) == (state.value, granted)
    assert decided["decided_by"] == "desk-3", "the record names the person, not the desk"
    assert bool(decided["result"]) is acted


# (why, the scopes the caller holds, the status)
DECIDERS = [
    ("an approver decides", ident.APPROVER_SCOPES, 200),
    ("reading is not deciding", frozenset({ident.SCOPE_APPROVALS_READ}), 403),
    ("an escalation reviewer may not", ident.REVIEWER_SCOPES, 403),
    ("a customer may not", ident.CUSTOMER_SCOPES, 403),
]


@pytest.mark.parametrize(("why", "scopes", "status"), DECIDERS, ids=[r[0] for r in DECIDERS])
@pytest.mark.discharges("AAC-0057", "AAC-0078")
async def test_deciding_needs_its_own_scope(why: str, scopes: frozenset[str], status: int) -> None:
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals)
        answered = await desk.post(
            f"/ops/approvals/{approval_id}/decide",
            json={"granted": True},
            headers=bearer(token(scopes=scopes)),
        )
    assert answered.status_code == status, answered.text


@pytest.mark.discharges("AAC-0078", "AAC-0057")
async def test_a_customer_cannot_grant_their_own_refund_even_holding_the_scope() -> None:
    """The scope is the first control and the workflow's validator is the
    second. A session that somehow held both is still refused, by the rule."""
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals)
        theirs = token(scopes=ident.APPROVER_SCOPES, subject=CUSTOMER, customer=CUSTOMER)
        answered = await desk.post(
            f"/ops/approvals/{approval_id}/decide", json={"granted": True}, headers=bearer(theirs)
        )
    assert answered.status_code == 409, answered.text
    assert "customer" in answered.json()["detail"]


# (why, whether it is decided first, the status the second decision gets)
REFUSALS = [
    ("an approval that does not exist", False, 404),
    ("one already decided", True, 409),
]


@pytest.mark.parametrize(("why", "first", "status"), REFUSALS, ids=[r[0] for r in REFUSALS])
@pytest.mark.discharges("AAC-0078")
async def test_a_refusal_the_workflow_makes_reaches_the_person_as_a_fact(
    why: str, first: bool, status: int
) -> None:
    async with desk_open() as (desk, approvals):
        approval_id = await waiting_refund(approvals) if first else "apr_nothing"
        if first:
            done = await desk.post(
                f"/ops/approvals/{approval_id}/decide", json={"granted": False}, headers=bearer()
            )
            assert done.status_code == 200, done.text
        answered = await desk.post(
            f"/ops/approvals/{approval_id}/decide", json={"granted": True}, headers=bearer()
        )

    assert answered.status_code == status, answered.text
    assert answered.json()["detail"], "a refusal says which one it is"


@pytest.mark.discharges("AHC-0010")
async def test_a_desk_with_no_approvals_wired_says_so_rather_than_answering_empty() -> None:
    """An empty queue and no queue are different facts, and a desk that confuses
    them tells an approver there is nothing to do."""
    async with desk_open(wired=False) as (desk, _):
        answered = await desk.get("/ops/approvals", headers=bearer())
    assert answered.status_code == 503


@pytest.mark.discharges("AHC-0010")
async def test_the_page_a_person_works_from_is_served_and_calls_only_the_api() -> None:
    """A desk that needs Swagger is a desk nobody uses. The page carries no
    data of its own: it is served unauthenticated and every call it makes is
    authorised on its own."""
    async with desk_open() as (desk, _):
        page = await desk.get("/ops/desk")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    body = page.text
    assert "/ops/approvals" in body and "/ops/escalations" in body, "both queues"
    assert "/decide" in body and "/resolve" in body, "and the buttons that decide them"
    assert "Bearer" in body, "the token travels in the header, not in the call it makes"


@pytest.mark.tooling
def test_the_desk_describes_both_queues_to_an_operator() -> None:
    """One mounted app, two surfaces, and the generated schema names both."""
    from support_agent import reviewer

    app: Any = reviewer.build(durable.RememberedEscalations(), issuer=ISSUER)
    paths = set(app.openapi()["paths"])
    assert {"/escalations", "/approvals", "/approvals/{approval_id}/decide"} <= paths
