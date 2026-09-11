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
from support_agent.llm import ScriptedClient
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

# (name, the order as the order system holds it, needs a person)
THRESHOLD_CASES = [
    ("returned, under the threshold", {"status": "returned", "total": 500}, False),
    ("returned, exactly at it", {"status": "returned", "total": 10000}, False),
    ("returned, over it", {"status": "returned", "total": "10000.01"}, True),
    ("returned, well over", {"status": "returned", "total": 99999}, True),
    ("returned, total missing", {"status": "returned"}, True),
    ("returned, total unreadable", {"status": "returned", "total": "about ten thousand"}, True),
    ("returned, total not a number", {"status": "returned", "total": "NaN"}, True),
    ("delivered and small — not owed", {"status": "delivered", "total": 500}, True),
    ("shipped and small — not owed", {"status": "shipped", "total": 500}, True),
]


@pytest.mark.parametrize(
    ("name", "order", "needs"), THRESHOLD_CASES, ids=[c[0] for c in THRESHOLD_CASES]
)
@pytest.mark.discharges("P-REFUND", "op:issue_refund")
def test_which_refunds_need_a_human(name: str, order: dict, needs: bool) -> None:
    """Every `agent_when` condition must hold for the agent to refund alone: the
    refund is owed, and the order's total is within the limit. An unreadable
    total needs approval — a gate that cannot read the number must not conclude
    the number is small."""
    reason = ap.requires_approval(order, ap.Policy())
    assert (reason is not None) is needs


# --------------------------------------------------------------------------- #
# Deciding. Three refusals, all fail closed.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0056", "AAC-0005", "AHC-0057")
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


@pytest.mark.discharges("AHC-0057", "AAC-0078")
async def test_an_expired_request_cannot_be_decided() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    with pytest.raises(ap.ApprovalError, match="expired"):
        await ap.decide(store, approval.id, granted=True, by="ops-7", now=T0 + 3 * 24 * HOUR)


async def test_deciding_an_unknown_approval_is_an_error() -> None:
    with pytest.raises(ap.ApprovalError, match="no approval"):
        await ap.decide(ap.InMemoryApprovalStore(), "apr_nope", granted=True, by="ops-7", now=T0)


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
@pytest.mark.discharges("AAC-0057", "AAC-0056", "AHC-0057", "AAC-0078")
async def test_the_elevated_scope_is_refused_without_a_live_grant(
    name: str, decided: bool, granted: bool, when: int
) -> None:
    store = ap.InMemoryApprovalStore()
    approval = await pending_refund(store)
    if decided:
        approval = await ap.decide(store, approval.id, granted=granted, by="ops-7", now=T0 + HOUR)
    with pytest.raises(ap.ApprovalError):
        ap.granted_identity(approval, customer(), now=when)


@pytest.mark.discharges("AHC-0057")
async def test_a_live_grant_mints_the_scope() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await ap.decide(
        store, (await pending_refund(store)).id, granted=True, by="ops-7", now=T0 + HOUR
    )
    elevated = ap.granted_identity(approval, customer(), now=T0 + HOUR)
    assert elevated.may(ident.SCOPE_REFUNDS_WRITE)
    assert not customer().may(ident.SCOPE_REFUNDS_WRITE)


@pytest.mark.discharges("AHC-0057")
async def test_an_approval_cannot_elevate_a_different_customer() -> None:
    store = ap.InMemoryApprovalStore()
    approval = await ap.decide(
        store, (await pending_refund(store)).id, granted=True, by="ops-7", now=T0 + HOUR
    )
    someone_else = Identity(customer_id="C-9999", scopes=ident.CUSTOMER_SCOPES)
    with pytest.raises(ap.ApprovalError, match="does not belong"):
        ap.granted_identity(approval, someone_else, now=T0 + HOUR)


@pytest.mark.discharges("AHC-0074")
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


class OrderOut(BaseModel):
    order_id: str
    status: str
    total: int | None = None


ORDERS = {
    "AB-1": OrderOut(order_id="AB-1", status="returned", total=12400),  # owed, large
    "AB-2": OrderOut(order_id="AB-2", status="returned", total=2400),  # owed, small
    "AB-3": OrderOut(order_id="AB-3", status="delivered", total=2400),  # small, not owed
    "AB-4": OrderOut(order_id="AB-4", status="returned"),  # owed, total unknown
}


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"refunds": 0, "refunded": []}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return ORDERS[order_id]

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_REFUNDS_WRITE,
        }
    )
    def issue_refund(order_id: str) -> RefundOut:
        """Refund an order, for its total. Irreversible."""
        srv.state["refunds"] += 1  # type: ignore[attr-defined]
        srv.state["refunded"].append(order_id)  # type: ignore[attr-defined]
        return RefundOut(refund_id=f"rf_{srv.state['refunds']}")  # type: ignore[attr-defined]

    return srv


@pytest.mark.discharges("AAC-0056", "AHC-0057")
async def test_a_large_refund_cannot_be_issued_before_it_is_granted(server) -> None:
    """The customer's own surface does not contain the tool at all."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        registry = await tools.list_tools(customer())
    assert registry.get("issue_refund") is None
    assert server.state["refunds"] == 0


@pytest.mark.discharges("AAC-0047", "AAC-0056", "AHC-0057", "AHC-0074", "op:issue_refund")
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
            await tools.call("issue_refund", {"order_id": "AB-1"}, elevated, ap.stored_key(granted))

    assert server.state["refunds"] == 1


@pytest.mark.discharges("AHC-0057")
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


# --------------------------------------------------------------------------- #
# A2's gate: through Agent.handle(), across turns.
# --------------------------------------------------------------------------- #


def refund_agent(tools, store, approvals, llm):
    from support_agent import entrypoint as ep

    return ep.build(llm=llm, tools=tools, store=store, approvals=approvals, clock=lambda: T0)


def wants_refund(order_id: str = "AB-1", **stated: object):
    """The model asks for a refund — and may state an amount, which the tool
    must ignore: the amount is the order's."""
    from support_agent.contracts import ModelResponse, ToolCall

    return ModelResponse(
        tool_calls=(
            ToolCall(id="tc", name=ap.REQUEST_REFUND, arguments={"order_id": order_id, **stated}),
        )
    )


@pytest.mark.discharges(
    "AAC-0056",
    "AAC-0047",
    "AHC-0057",
    "AAC-0078",
    "P-REFUND",
    "op:request_refund",
    "ext:approval_queue",
)
async def test_a_large_refund_waits_and_then_completes_across_turns(server) -> None:
    """The whole gate, through the drivable surface.

    Turn one asks and is told nothing has been refunded. A colleague authorises
    it. Turn two resumes and completes — and the customer is never told a refund
    happened before it did.
    """
    from support_agent.contracts import Completed, NeedsApproval
    from support_agent.state import InMemoryCheckpointStore

    approvals = ap.InMemoryApprovalStore()
    ledger = InMemoryLedger()

    async with connect(server, ledger=ledger) as tools:
        agent = refund_agent(
            tools,
            InMemoryCheckpointStore(),
            approvals,
            ScriptedClient([wants_refund()]),
        )

        first, conversation = await agent.handle(
            "I want my money back for AB-1", identity=customer()
        )
        assert isinstance(first, NeedsApproval)
        assert "Nothing has been refunded" in first.reply
        assert server.state["refunds"] == 0
        assert conversation.pending_approval_id == first.approval_id

        await ap.decide(approvals, first.approval_id, granted=True, by="ops-7", now=T0 + 1)

        agent.llm = ScriptedClient([])  # a resume must not need the model
        second, conversation = await agent.handle(
            "any news?", identity=customer(), conversation=conversation
        )

    assert isinstance(second, Completed)
    assert server.state["refunds"] == 1
    assert conversation.pending_approval_id is None


@pytest.mark.discharges("AHC-0057", "ext:approval_queue")
async def test_a_pending_approval_short_circuits_the_next_turn(server) -> None:
    from support_agent.contracts import Completed, NeedsApproval
    from support_agent.state import InMemoryCheckpointStore

    approvals = ap.InMemoryApprovalStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = refund_agent(
            tools, InMemoryCheckpointStore(), approvals, ScriptedClient([wants_refund()])
        )
        first, conversation = await agent.handle("refund AB-1 please", identity=customer())
        assert isinstance(first, NeedsApproval)

        agent.llm = ScriptedClient([])
        second, _ = await agent.handle("well?", identity=customer(), conversation=conversation)

    assert isinstance(second, Completed)
    assert "still with a colleague" in second.reply
    assert server.state["refunds"] == 0


@pytest.mark.discharges("AHC-0057")
async def test_a_refused_approval_is_reported_and_nothing_is_refunded(server) -> None:
    from support_agent.contracts import Completed
    from support_agent.state import InMemoryCheckpointStore

    approvals = ap.InMemoryApprovalStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = refund_agent(
            tools, InMemoryCheckpointStore(), approvals, ScriptedClient([wants_refund()])
        )
        first, conversation = await agent.handle("refund AB-1", identity=customer())
        await ap.decide(approvals, first.approval_id, granted=False, by="ops-7", now=T0 + 1)

        agent.llm = ScriptedClient([])
        second, conversation = await agent.handle(
            "and now?", identity=customer(), conversation=conversation
        )

    assert isinstance(second, Completed)
    assert "could not authorise" in second.reply
    assert server.state["refunds"] == 0
    assert conversation.pending_approval_id is None


@pytest.mark.discharges("AHC-0057", "AAC-0056")
async def test_an_agent_without_an_approval_store_cannot_refund_at_all(server) -> None:
    """It does not fall back to issuing one — the tool is simply not advertised."""
    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([wants_refund(), _says()]),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        await agent.handle("refund AB-1", identity=customer())
    assert server.state["refunds"] == 0


def _says():
    from support_agent.contracts import ModelResponse

    return ModelResponse(text="I cannot do that.")


# (name, seconds after the request the next turn resumes, the result it must be)
RESUMES = [
    ("inside the approval's life, the grant executes", HOUR, "Completed"),
    ("past its life, the grant is refused as expired", 24 * HOUR + 1, "Failed"),
]


@pytest.mark.discharges("AHC-0057", "AHC-0014", "op:request_refund")
@pytest.mark.parametrize(("name", "later", "outcome"), RESUMES, ids=[r[0] for r in RESUMES])
async def test_an_approval_lives_on_the_agents_clock(
    server, name: str, later: int, outcome: str
) -> None:
    """F-021: the request tool and the resume check read the wall clock, so an
    approval's life was not the agent's to control — and a replay minted a
    different expiry every time."""
    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    moment = [T0]
    approvals = ap.InMemoryApprovalStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([wants_refund()]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            clock=lambda: moment[0],
        )
        first, conversation = await agent.handle("refund AB-1 please", identity=customer())
        requested = await approvals.get(first.approval_id)
        assert requested.expires_at == T0 + 24 * HOUR, "the expiry is the agent's, not the wall's"

        await ap.decide(approvals, first.approval_id, granted=True, by="ops-7", now=T0 + 1)
        moment[0] = T0 + later
        agent.llm = ScriptedClient([])
        second, _ = await agent.handle("any news?", identity=customer(), conversation=conversation)

    assert type(second).__name__ == outcome, second


# --------------------------------------------------------------------------- #
# F-014 — the amount is the order's, never the conversation's.
# --------------------------------------------------------------------------- #


class RecordingApprovals(ap.InMemoryApprovalStore):
    """Every approval row as last written, decided or not."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, ap.Approval] = {}

    async def put(self, approval: ap.Approval) -> None:
        self.rows[approval.id] = approval
        await super().put(approval)


ISSUED = "Your refund has been issued to your original payment method."

# (name, order, what the model states, result type, refunded, stored amount, granted by)
GROUNDING = [
    ("an understated amount on a large order still needs a person",
     "AB-1", {"amount": "500"}, "NeedsApproval", [], "12400", None),
    ("a small owed refund is issued at once",
     "AB-2", {}, "Completed", ["AB-2"], "2400", ap.POLICY_APPROVER),
    ("an overstated amount on a small order is refunded for its total",
     "AB-2", {"amount": "99999"}, "Completed", ["AB-2"], "2400", ap.POLICY_APPROVER),
    ("a small refund that is not owed goes to a person",
     "AB-3", {}, "NeedsApproval", [], "2400", None),
    ("a total nobody can read goes to a person",
     "AB-4", {}, "NeedsApproval", [], None, None),
]  # fmt: skip


@pytest.mark.discharges("P-REFUND", "op:request_refund", "op:issue_refund", "AHC-0057")
@pytest.mark.parametrize(
    ("name", "order", "stated", "outcome", "refunded", "amount", "granted_by"),
    GROUNDING,
    ids=[g[0] for g in GROUNDING],
)
async def test_a_refund_is_for_the_orders_total_and_the_gate_reads_it(
    server,
    name: str,
    order: str,
    stated: dict,
    outcome: str,
    refunded: list,
    amount: str | None,
    granted_by: str | None,
) -> None:
    """F-014: the threshold compared a number the model passed, so a customer who
    understated a large refund talked it under the gate. Now the tool takes no
    amount; it reads the order's total from the order system, and every refund —
    automatic or not — leaves an approval row naming who authorised it."""
    from support_agent.contracts import ModelResponse
    from support_agent.state import InMemoryCheckpointStore

    approvals = RecordingApprovals()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = refund_agent(
            tools,
            InMemoryCheckpointStore(),
            approvals,
            ScriptedClient([wants_refund(order, **stated), ModelResponse(text=ISSUED)]),
        )
        result, _ = await agent.handle(f"please refund {order}", identity=customer())

    assert type(result).__name__ == outcome, result
    assert server.state["refunded"] == refunded
    (row,) = approvals.rows.values()
    assert row.args["amount"] == amount, "the stored amount is the order's total"
    assert row.decided_by == granted_by
    if refunded:
        assert result.reply == ISSUED, "a refund that happened may be said to have happened"
