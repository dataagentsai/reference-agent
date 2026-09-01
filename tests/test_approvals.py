"""The human gate.

The end-to-end test at the bottom is the one that matters: a refund above the
threshold waits, is granted an hour later by someone else, resumes, and produces
**exactly one** refund row — even when the resume is attempted twice.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import IdempotencyKey, Identity, RunId, SideEffectClass
from support_agent.idempotency import InMemoryLedger
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT, connect

T0 = 1_000_000
HOUR = 3600
RUN = RunId("run_ap")


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def key() -> IdempotencyKey:
    return IdempotencyKey(run_id=RUN, step=2, iteration=1)


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


async def pending_refund(store, amount: str = "12400") -> ap.Approval:
    return await ap.request(
        store,
        action=ap.REFUND_ACTION,
        args={"order_id": "AB-1", "amount": amount},
        reason="above threshold",
        identity=customer(),
        idempotency_key=key(),
        now=T0,
    )


# --------------------------------------------------------------------------- #
# What needs a human.
# --------------------------------------------------------------------------- #

THRESHOLD_CASES = [
    ("under the threshold", {"amount": "500"}, False),
    ("exactly at it", {"amount": "10000"}, False),
    ("over it", {"amount": "10000.01"}, True),
    ("well over", {"amount": "99999"}, True),
    ("amount missing", {}, True),
    ("amount unreadable", {"amount": "about ten thousand"}, True),
]


@pytest.mark.parametrize(
    ("name", "args", "needs"), THRESHOLD_CASES, ids=[c[0] for c in THRESHOLD_CASES]
)
def test_which_refunds_need_a_human(name: str, args: dict, needs: bool) -> None:
    """An unreadable amount needs approval. A gate that cannot read the number
    must not conclude the number is small."""
    reason = ap.requires_approval(ap.REFUND_ACTION, args, ap.Policy())
    assert (reason is not None) is needs


def test_only_refunds_are_gated() -> None:
    assert ap.requires_approval("get_order", {"order_id": "AB-1"}, ap.Policy()) is None


# --------------------------------------------------------------------------- #
# Deciding. Three refusals, all fail closed.
# --------------------------------------------------------------------------- #


async def test_nobody_approves_their_own_request() -> None:
    """The confused deputy of the human path, and the control against "your
    colleague already approved this" — the claim would have to be true in the
    store, and the store is not persuadable."""
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    with pytest.raises(ap.ApprovalError, match="cannot be granted by the customer"):
        await ap.decide(store, approval.id, granted=True, by="C-1042", now=T0 + HOUR)


async def test_a_decision_is_terminal() -> None:
    """A grant that can be re-granted is a grant that can be executed twice."""
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + HOUR)
    with pytest.raises(ap.ApprovalError, match="already decided"):
        await ap.decide(store, approval.id, granted=False, by="ops-7", now=T0 + HOUR)


async def test_an_expired_request_cannot_be_decided() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    with pytest.raises(ap.ApprovalError, match="expired"):
        await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + 3 * 24 * HOUR)


async def test_deciding_an_unknown_approval_is_an_error() -> None:
    with pytest.raises(ap.ApprovalError, match="no approval"):
        await ap.decide(ap.InMemoryApprovalStore(), "apr_nope", granted=True, by="ops-7")


async def test_the_queue_shows_only_undecided_items() -> None:
    """P8 — the surface a reviewer sees."""
    store = ap.InMemoryApprovalStore()
    first = await pending_refund(store)
    await pending_refund(store)
    assert len(await store.pending()) == 2
    await ap.decide(store, first.id, granted=True, by="ops-7", now=T0 + HOUR)
    assert len(await store.pending()) == 1


# --------------------------------------------------------------------------- #
# Privilege separation. The elevated scope exists in exactly one place.
# --------------------------------------------------------------------------- #

ELEVATION_CASES = [
    ("pending", False, False, T0 + HOUR),
    ("refused", True, False, T0 + HOUR),
    ("expired grant", True, True, T0 + 3 * 24 * HOUR),
]


@pytest.mark.parametrize(
    ("name", "decided", "granted", "when"),
    ELEVATION_CASES,
    ids=[c[0] for c in ELEVATION_CASES],
)
async def test_the_elevated_scope_is_refused_without_a_live_grant(
    name: str, decided: bool, granted: bool, when: int
) -> None:
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    if decided:
        approval = await ap.decide(store, approval.id, granted=granted, by="ops-7", now=T0 + HOUR)
    with pytest.raises(ap.ApprovalError):
        ap.granted_identity(approval, customer(), now=when)


async def test_a_live_grant_mints_the_scope() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await ap.decide(
        store, (await pending_refund(store)).id, granted=True, by="ops-7", now=T0 + HOUR
    )
    elevated = ap.granted_identity(approval, customer(), now=T0 + HOUR)
    assert elevated.may(ident.SCOPE_REFUNDS_WRITE)
    assert not customer().may(ident.SCOPE_REFUNDS_WRITE)


async def test_an_approval_cannot_elevate_a_different_customer() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await ap.decide(
        store, (await pending_refund(store)).id, granted=True, by="ops-7", now=T0 + HOUR
    )
    someone_else = Identity(customer_id="C-9999", scopes=ident.CUSTOMER_SCOPES)
    with pytest.raises(ap.ApprovalError, match="does not belong"):
        ap.granted_identity(approval, someone_else, now=T0 + HOUR)


async def test_the_original_key_survives_the_wait() -> None:
    """L14 meets L10. Executing under a fresh key would defeat the ledger."""
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    assert ap.stored_key(approval) == key()


# --------------------------------------------------------------------------- #
# End to end. The gate holds and the effect happens once.
# --------------------------------------------------------------------------- #


class RefundOut(BaseModel):
    refund_id: str


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"refunds": 0}  # type: ignore[attr-defined]

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_REFUNDS_WRITE,
        }
    )
    def issue_refund(order_id: str, amount: str) -> RefundOut:
        """Refund an order. Irreversible."""
        srv.state["refunds"] += 1  # type: ignore[attr-defined]
        return RefundOut(refund_id=f"rf_{srv.state['refunds']}")  # type: ignore[attr-defined]

    return srv


async def test_a_large_refund_cannot_be_issued_before_it_is_granted(server) -> None:
    """The customer's own surface does not contain the tool at all."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        registry = await tools.list_tools(customer())
    assert registry.get("issue_refund") is None
    assert server.state["refunds"] == 0


async def test_the_whole_path_produces_exactly_one_refund(server) -> None:
    """Requested, waited, granted an hour later by someone else, resumed — and
    resumed *twice*, because a duplicate click and a retried process are the same
    thing to a ledger."""
    store = ap.InMemoryApprovalStore()
    ledger = InMemoryLedger()

    approval = await pending_refund(store)
    assert server.state["refunds"] == 0

    granted = await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + HOUR)
    elevated = ap.granted_identity(granted, customer(), now=T0 + HOUR)

    async with connect(server, ledger=ledger) as tools:
        for _ in range(2):
            await tools.call(
                "issue_refund",
                {"order_id": "AB-1", "amount": "12400"},
                elevated,
                ap.stored_key(granted),
            )

    assert server.state["refunds"] == 1


async def test_a_refused_approval_never_reaches_the_tool(server) -> None:
    store = ap.InMemoryApprovalStore()
    refused = await ap.decide(
        store, (await pending_refund(store)).id, granted=False, by="ops-7", now=T0 + HOUR
    )
    with pytest.raises(ap.ApprovalError):
        ap.granted_identity(refused, customer(), now=T0 + HOUR)
    assert server.state["refunds"] == 0


async def test_the_decision_is_on_the_trace(exporter) -> None:
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + HOUR)
    names = [s.name for s in exporter.get_finished_spans()]
    assert "agent.approval.request" in names
    assert "agent.approval.decide" in names
