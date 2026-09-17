"""T-002 (C): the order system decides who is asking, and on whose approval.

The agent sends a token exchanged for the order system and, for a refund, the
approval that elevated it. The projected shop runs `order_system.authoriser`,
the check a real store runs, so every row here goes through the same transport
a deployment uses. Each row states what the far end must conclude and checks
the world, not only the reply: a refusal that still moved a row is not one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project
from evals import durable
from evals import issuer as issuing

from order_system import authoriser
from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.binding import SCOPES
from support_agent.contracts import Approval, IdempotencyKey, Identity, RunId
from support_agent.idempotency import InMemoryLedger
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
T0 = 1_000_000
HOUR = 3600
ORDER = "AB-10003"  # C-1042's, delivered
KEY = IdempotencyKey(run_id=RunId("run_far"), step=2, iteration=1)


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def session(customer: str) -> Identity:
    """A customer's session as the chat edge hands it on: verified, with its token."""
    return ident.verify(issuing.mint(customer), issuer=issuing.issuer()).as_customer()


class PassThrough:
    """Sends the customer's own session, addressed to the agent, not the far end."""

    async def for_far_end(self, identity: Identity) -> str:
        return identity.token or ""


async def call(caller: Identity, operation: str, arguments: dict, *, exchange, approvals):
    world = Live.start(load(WORLD))
    check = authoriser(
        issuer=issuing.issuer(audience="order-system"),
        approvals=approvals,
        required_scopes=SCOPES,
        clock=lambda: T0 + HOUR,
    )
    server = project(world, scopes=SCOPES, authorise=check)
    async with connect(server, ledger=InMemoryLedger(), exchange=exchange) as tools:
        result = await tools.call(operation, arguments, caller, KEY)
    return result, world


# (why, the caller, how its session reaches the far end, whether the order is served)
READS = [
    ("the owner, through an exchanged token", lambda: session("C-1042"), "exchange", True),
    ("a stranger, through an exchanged token", lambda: session("C-9999"), "exchange", False),
    (
        "a stranger asserting the owner's customer id",
        lambda: session("C-9999").model_copy(update={"customer_id": "C-1042"}),
        "exchange",
        False,
    ),
    ("the owner, asserted with no token at all", lambda: session("C-1042"), None, False),
    (
        "the owner's own session, not exchanged for this audience",
        lambda: session("C-1042"),
        "pass-through",
        False,
    ),
]


@pytest.mark.parametrize(("why", "caller", "via", "served"), READS, ids=[r[0] for r in READS])
@pytest.mark.discharges("P-OWNERSHIP", "AAC-0057", "AAC-0111", "AHC-0099")
async def test_the_far_end_believes_the_token_and_not_the_assertion(
    ap_store, why: str, caller, via: str | None, served: bool
) -> None:
    exchange = {"exchange": issuing.LocalExchange(), "pass-through": PassThrough(), None: None}[via]
    result, _ = await call(
        caller(), "get_order", {"id": ORDER}, exchange=exchange, approvals=ap_store
    )
    assert (not result.is_error) is served, result.text


@pytest.fixture
def ap_store() -> durable.Remembered:
    """Approvals as records to check against: the far end only ever reads them."""
    return durable.Remembered()


def approval(**changes: object) -> Approval:
    """A refund of ORDER, requested by C-1042 under KEY, granted by a reviewer."""
    base = Approval(
        id="apr_far",
        action="issue_refund",
        args={"order_id": ORDER, "amount": "4999"},
        reason="over the threshold",
        customer_id="C-1042",
        idempotency_key=KEY.value,
        created_at=T0,
        expires_at=T0 + 2 * HOUR,
        decided=True,
        granted=True,
        decided_by="desk-1",
    )
    return base.model_copy(update=changes)


# (why, the stored approval or None, whether the refund lands)
REFUNDS = [
    ("a granted approval that matches the call", approval(), True),
    ("no approval named", None, False),
    ("still pending", approval(decided=False, granted=False, decided_by=None), False),
    ("refused by the reviewer", approval(granted=False), False),
    ("expired before it was used", approval(expires_at=T0 + HOUR), False),
    (
        "approved for another order",
        approval(args={"order_id": "AB-10001", "amount": "1899"}),
        False,
    ),
    ("approved for another customer", approval(customer_id="C-9999"), False),
    ("approved for another operation", approval(action="cancel_order"), False),
    ("approved by the customer themself", approval(decided_by="C-1042"), False),
    ("requested as another call", approval(idempotency_key="run_other:2:1"), False),
]


@pytest.mark.parametrize(("why", "stored", "lands"), REFUNDS, ids=[r[0] for r in REFUNDS])
@pytest.mark.discharges("AAC-0057", "AHC-0057", "P-OWNERSHIP")
async def test_a_refund_lands_only_on_an_approval_the_far_end_checked(
    ap_store, why: str, stored: Approval | None, lands: bool
) -> None:
    """The agent's elevated identity is the same in every row: `refunds:write`
    and a grant naming `apr_far`. What differs is the record the far end loads,
    and only that decides."""
    if stored is not None:
        ap_store.add(stored)
    elevated = session("C-1042").model_copy(
        update={"scopes": ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE}, "grant": "apr_far"}
    )
    result, world = await call(
        elevated,
        "issue_refund",
        {"id": ORDER},
        exchange=issuing.LocalExchange(),
        approvals=ap_store,
    )

    refunded = ("issue_refund", ORDER) in world.effects
    assert refunded is lands, result.text
    assert (not result.is_error) is lands


@pytest.mark.discharges("AAC-0057")
async def test_the_agents_own_elevation_is_not_sent_as_authority(ap_store) -> None:
    """The exchanged token carries what the customer's session carries. The
    scope the agent added in process never reaches the far end as a claim."""
    exchange = issuing.LocalExchange()
    elevated = session("C-1042").model_copy(
        update={"scopes": ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE}}
    )
    token = await exchange.for_far_end(elevated)
    far = ident.verify(token, issuer=issuing.issuer(audience="order-system"))
    assert ident.SCOPE_REFUNDS_WRITE not in far.scopes
    assert (far.customer_id, far.party) == ("C-1042", "support-agent")


@pytest.mark.discharges("AAC-0057")
async def test_granted_identity_names_its_approval() -> None:
    granted = ap.granted_identity(approval(), session("C-1042"), now=T0 + HOUR)
    assert granted.grant == "apr_far"
