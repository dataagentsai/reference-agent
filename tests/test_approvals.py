"""The human gate.

The end-to-end tests are the ones that matter: a refund above the threshold
waits in a Temporal workflow, is granted an hour later by someone else — across a
restart of the worker — and produces **exactly one** refund, even when it is
granted twice. The workflow runs on Temporal's test server, the real engine in
memory (T-028).
"""

from __future__ import annotations

import time

import pytest
from evals import durable
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import IdempotencyKey, Identity, RunId, SideEffectClass
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT, connect

T0 = int(time.time())
"""Now, because the workflow's clock is Temporal's and starts from the wall."""
HOUR = 3600
DAY = 24 * HOUR
RUN = RunId("run_ap")


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def key() -> IdempotencyKey:
    return IdempotencyKey(run_id=RUN, step=2, iteration=1)


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def record(**changes: object) -> ap.Approval:
    """An approval as the workflow holds it once a person must decide."""
    base = ap.Approval(
        id="apr_1",
        action=ap.REFUND_ACTION,
        args={"order_id": "AB-201", "amount": "12400"},
        reason="above threshold",
        customer_id="C-1042",
        idempotency_key=key().value,
        created_at=T0,
        expires_at=T0 + DAY,
        state=ap.ApprovalState.WAITING,
    )
    return base.model_copy(update=changes)


def granted(**changes: object) -> ap.Approval:
    return record(decided=True, granted=True, decided_by="ops-7", **changes)


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
# Who may decide. The workflow runs this as its update's validator.
# --------------------------------------------------------------------------- #

# (why, the approval, who decides, the customer that login is, when, refused with)
REFUSALS = [
    ("a colleague within the window", record(), "ops-7", None, T0 + HOUR, None),
    ("the customer themself", record(), "C-1042", None, T0 + HOUR,
     "cannot be granted by the customer"),
    ("a login linked to the customer", record(), "kc-user-9", "C-1042", T0 + HOUR,
     "cannot be granted by the customer"),
    ("already decided", granted(), "ops-8", None, T0 + HOUR, "already decided"),
    ("past its window", record(), "ops-7", None, T0 + DAY, "expired"),
    ("expired by the workflow", record(state=ap.ApprovalState.EXPIRED), "ops-7", None, T0,
     "expired"),
    ("still being assessed", record(state=ap.ApprovalState.ASSESSING), "ops-7", None, T0,
     "being assessed"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "approval", "by", "by_customer", "now", "refused"),
    REFUSALS,
    ids=[r[0] for r in REFUSALS],
)
@pytest.mark.discharges(
    "AAC-0056",
    "AAC-0005",
    "AHC-0057",
    "AAC-0078",
    "P-APPROVER",
    "P-APPROVAL-FINAL",
    "P-APPROVAL-TTL",
)
def test_who_may_decide_and_when(
    why: str, approval: ap.Approval, by: str, by_customer: str | None, now: int, refused: str | None
) -> None:
    """Three refusals, all fail closed: nobody approves their own request (the
    confused deputy of the human path), a decision is terminal (a grant that
    can be re-granted can be executed twice), and an expired request cannot be
    decided."""
    why_not = ap.refusal(approval, by=by, by_customer=by_customer, now=now)
    if refused is None:
        assert why_not is None
    else:
        assert why_not is not None and refused in why_not, why_not


# --------------------------------------------------------------------------- #
# Privilege separation. The elevated scope exists in exactly one place.
# --------------------------------------------------------------------------- #

# (why, the approval, whose identity, when, mints the scope)
ELEVATION_CASES = [
    ("a live grant", granted(), customer(), T0 + HOUR, True),
    ("pending", record(), customer(), T0 + HOUR, False),
    ("refused", record(decided=True, granted=False), customer(), T0 + HOUR, False),
    ("an expired grant", granted(), customer(), T0 + 3 * DAY, False),
    ("another customer", granted(), Identity(customer_id="C-9999"), T0 + HOUR, False),
]


@pytest.mark.parametrize(
    ("why", "approval", "base", "when", "mints"),
    ELEVATION_CASES,
    ids=[c[0] for c in ELEVATION_CASES],
)
@pytest.mark.discharges("AAC-0057", "AAC-0056", "AHC-0057", "AAC-0078")
def test_the_elevated_scope_needs_a_live_grant_for_this_customer(
    why: str, approval: ap.Approval, base: Identity, when: int, mints: bool
) -> None:
    if not mints:
        with pytest.raises(ap.ApprovalError):
            ap.granted_identity(approval, base, now=when)
        return
    elevated = ap.granted_identity(approval, base, now=when)
    assert elevated.may(ident.SCOPE_REFUNDS_WRITE) and elevated.grant == approval.id
    assert not base.may(ident.SCOPE_REFUNDS_WRITE)


@pytest.mark.discharges("AHC-0074")
def test_the_original_key_survives_the_wait() -> None:
    """L14 meets L10. Executing under a fresh key would defeat the ledger."""
    assert ap.stored_key(record()) == key()


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
    "AB-201": OrderOut(order_id="AB-201", status="returned", total=12400),  # owed, large
    "AB-202": OrderOut(order_id="AB-202", status="returned", total=2400),  # owed, small
    "AB-203": OrderOut(order_id="AB-203", status="delivered", total=2400),  # small, not owed
    "AB-204": OrderOut(order_id="AB-204", status="returned"),  # owed, total unknown
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
    async with connect(server, requests=InMemoryRequests()) as tools:
        registry = await tools.list_tools(customer())
    assert registry.get("issue_refund") is None
    assert server.state["refunds"] == 0


async def ask(approvals, order: str = "AB-201") -> ap.Approval:
    return await approvals.request(
        action=ap.REFUND_ACTION,
        args={"order_id": order},
        identity=customer(),
        idempotency_key=key(),
    )


# (why, the reviewers' decisions in order, what each is told, the state, refunds)
DECISIONS = [
    ("granted, the refund is issued at once", [("ops-7", True)], [None], "done", 1),
    ("refused, nothing reaches the tool", [("ops-7", False)], [None], "refused", 0),
    ("granted twice, the second is refused and money moves once",
     [("ops-7", True), ("ops-8", True)], [None, "already decided"], "done", 1),
    ("the customer cannot grant their own", [("C-1042", True)],
     ["cannot be granted by the customer"], "waiting", 0),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "decisions", "told", "state", "refunds"), DECISIONS, ids=[d[0] for d in DECISIONS]
)
@pytest.mark.discharges(
    "AAC-0047", "AAC-0056", "AHC-0057", "AHC-0074", "P-APPROVAL-FINAL", "op:issue_refund"
)
async def test_a_decision_is_acted_on_by_the_workflow(
    server, why: str, decisions: list, told: list, state: str, refunds: int
) -> None:
    """The workflow, not the agent, carries out a grant, the moment it is
    given and under the key it was requested with (T-028)."""
    moment = [T0]
    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools, clock=lambda: moment[0]) as waits,
    ):
        approval = await ask(waits.approvals)
        assert approval.state is ap.ApprovalState.WAITING
        moment[0] = T0 + HOUR
        for (by, grant), expected in zip(decisions, told, strict=True):
            if expected is None:
                await waits.desk.decide(approval.id, granted=grant, by=by, now=moment[0])
                continue
            with pytest.raises(ap.ApprovalError, match=expected):
                await waits.desk.decide(approval.id, granted=grant, by=by, now=moment[0])
        final = await waits.approvals.get(approval.id)

    assert final is not None and final.state.value == state
    assert server.state["refunds"] == refunds


# (why, how long after the request someone decides, refused with)
TOO_LATE = [
    ("inside the window", HOUR, None),
    # Clear of the boundary: the window opens when the workflow starts, which
    # is a moment after this test read the clock.
    ("after the window", DAY + 300, "has expired"),
    ("an approval nobody raised", HOUR, "no approval"),
]


@pytest.mark.parametrize(("why", "later", "refused"), TOO_LATE, ids=[t[0] for t in TOO_LATE])
@pytest.mark.discharges("AHC-0057", "AAC-0078", "P-APPROVAL-TTL", "AHC-0018")
async def test_an_approval_expires_on_the_workflows_clock(
    server, why: str, later: int, refused: str | None
) -> None:
    moment = [T0]
    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools, clock=lambda: moment[0]) as waits,
    ):
        approval = await ask(waits.approvals)
        target = "apr_nobody" if "nobody" in why else approval.id
        moment[0] = T0 + later
        if refused is None:
            await waits.desk.decide(target, granted=True, by="ops-7", now=moment[0])
        else:
            with pytest.raises(ap.ApprovalError, match=refused):
                await waits.desk.decide(target, granted=True, by="ops-7", now=moment[0])
    assert server.state["refunds"] == (1 if refused is None else 0)


@pytest.mark.discharges("AHC-0057", "P-APPROVAL-WAIT", "op:issue_refund")
async def test_a_restart_during_an_hour_long_approval_resumes_it(server) -> None:
    """T-028's done-when. The worker that raised the approval stops; an hour
    later a different worker, with a new connection to the order system, is
    running when the colleague grants it, and the refund is issued once."""
    moment = [T0]
    async with durable.server(clock=lambda: moment[0]) as waits:
        async with connect(server, requests=InMemoryRequests()) as tools, waits.worker(tools):
            approval = await ask(waits.approvals)
        assert server.state["refunds"] == 0

        moment[0] = T0 + HOUR
        async with connect(server, requests=InMemoryRequests()) as tools, waits.worker(tools):
            done = await waits.desk.decide(approval.id, granted=True, by="ops-7", now=moment[0])

    assert done.state is ap.ApprovalState.DONE
    assert server.state["refunds"] == 1


@pytest.mark.discharges("P-APPROVAL-QUEUE")
async def test_the_queue_shows_only_what_waits_for_a_person(server) -> None:
    """P8 — the surface a reviewer sees. A small owed refund never reaches it."""
    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools) as waits,
    ):
        first = await ask(waits.approvals, "AB-201")
        await ask(waits.approvals, "AB-204")
        await ask(waits.approvals, "AB-202")
        assert {a.args["order_id"] for a in await waits.approvals.pending()} == {"AB-201", "AB-204"}
        await waits.desk.decide(first.id, granted=True, by="ops-7")
        assert [a.args["order_id"] for a in await waits.approvals.pending()] == ["AB-204"]


@pytest.mark.discharges("AHC-0018")
async def test_the_decision_is_on_the_trace(server, exporter) -> None:
    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools) as waits,
    ):
        approval = await ask(waits.approvals)
        await waits.desk.decide(approval.id, granted=True, by="ops-7")
    names = [s.name for s in exporter.get_finished_spans()]
    for name in ("agent.approval.request", "agent.approval.decide", "agent.approval.carry_out"):
        assert name in names


# --------------------------------------------------------------------------- #
# A2's gate: through Agent.handle(), across turns.
# --------------------------------------------------------------------------- #


def refund_agent(tools, approvals, llm, clock=lambda: T0):
    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    return ep.build(
        llm=llm, tools=tools, store=InMemoryCheckpointStore(), approvals=approvals, clock=clock
    )


def wants_refund(order_id: str = "AB-201", **stated: object):
    """The model asks for a refund — and may state an amount, which the tool
    must ignore: the amount is the order's."""
    from support_agent.contracts import ModelResponse, ToolCall

    return ModelResponse(
        tool_calls=(
            ToolCall(id="tc", name=ap.REQUEST_REFUND, arguments={"order_id": order_id, **stated}),
        )
    )


# (why, what the colleague does, seconds before they do, refunds before the
#  customer speaks again, what the next turn is, what it says)
ACROSS_TURNS = [
    ("granted: refunded before the customer asks, and told so", True, HOUR, 1,
     "Completed", "refund is on its way"),
    ("refused: nothing refunded, and told so", False, HOUR, 0,
     "Completed", "could not authorise"),
    ("nobody yet: still with a colleague", None, HOUR, 0,
     "Completed", "still with a colleague"),
    ("nobody in time: the authorisation lapsed", None, DAY + 300, 0,
     "Failed", "no longer valid"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "grant", "later", "refunds", "outcome", "says"),
    ACROSS_TURNS,
    ids=[a[0] for a in ACROSS_TURNS],
)
@pytest.mark.discharges(
    "AAC-0056",
    "AAC-0047",
    "AHC-0057",
    "AAC-0078",
    "P-REFUND",
    "op:request_refund",
    "ext:approval_queue",
    "P-APPROVAL-WAIT",
)
async def test_a_large_refund_waits_and_the_next_turn_says_what_became_of_it(
    server, why: str, grant: bool | None, later: int, refunds: int, outcome: str, says: str
) -> None:
    """The whole gate, through the drivable surface.

    Turn one asks and is told nothing has been refunded. A colleague decides,
    or does not, and a granted refund is issued then, not when the customer
    next speaks. Turn two only reports it, without the model.
    """
    from support_agent.contracts import NeedsApproval

    moment = [T0]
    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools, clock=lambda: moment[0]) as waits,
    ):
        agent = refund_agent(
            tools, waits.approvals, ScriptedClient([wants_refund()]), clock=lambda: moment[0]
        )
        first, conversation = await agent.handle(
            "I want my money back for AB-201", identity=customer()
        )
        assert isinstance(first, NeedsApproval)
        assert "Nothing has been refunded" in first.reply
        assert conversation.pending_approval_id == first.approval_id

        moment[0] = T0 + later
        if grant is not None:
            await waits.desk.decide(first.approval_id, granted=grant, by="ops-7", now=moment[0])
        assert server.state["refunds"] == refunds, "the grant acts, not the next turn"

        agent.llm = ScriptedClient([])  # a resume must not need the model
        second, conversation = await agent.handle(
            "any news?", identity=customer(), conversation=conversation
        )

    assert type(second).__name__ == outcome, second
    text = getattr(second, "reply", "") or getattr(second, "customer_message", "")
    assert says in text
    assert server.state["refunds"] == refunds
    waiting = outcome == "Completed" and grant is None
    assert (conversation.pending_approval_id is not None) is waiting


@pytest.mark.discharges("AHC-0057", "AAC-0056")
async def test_an_agent_without_approvals_cannot_refund_at_all(server) -> None:
    """It does not fall back to issuing one — the tool is simply not advertised."""
    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=ScriptedClient([wants_refund(), _says()]),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        await agent.handle("refund AB-201", identity=customer())
    assert server.state["refunds"] == 0


def _says():
    from support_agent.contracts import ModelResponse

    return ModelResponse(text="I cannot do that.")


# --------------------------------------------------------------------------- #
# F-014 — the amount is the order's, never the conversation's.
# --------------------------------------------------------------------------- #


class Recording:
    """The agent's approvals, remembering which it asked for."""

    def __init__(self, inner) -> None:
        self.inner, self.asked = inner, []
        self.durable = inner.durable

    async def request(self, **kwargs):
        approval = await self.inner.request(**kwargs)
        self.asked.append(approval.id)
        return approval

    async def get(self, approval_id):
        return await self.inner.get(approval_id)

    async def pending(self):
        return await self.inner.pending()


ISSUED = "Your refund has been issued to your original payment method."

# (name, order, what the model states, result type, refunded, stored amount, granted by)
GROUNDING = [
    ("an understated amount on a large order still needs a person",
     "AB-201", {"amount": "500"}, "NeedsApproval", [], "12400", None),
    ("a small owed refund is issued at once",
     "AB-202", {}, "Completed", ["AB-202"], "2400", ap.POLICY_APPROVER),
    ("an overstated amount on a small order is refunded for its total",
     "AB-202", {"amount": "99999"}, "Completed", ["AB-202"], "2400", ap.POLICY_APPROVER),
    ("a small refund that is not owed goes to a person",
     "AB-203", {}, "NeedsApproval", [], "2400", None),
    ("a total nobody can read goes to a person",
     "AB-204", {}, "NeedsApproval", [], None, None),
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
    amount; the workflow reads the order's total from the order system, and
    every refund — automatic or not — has an approval naming who authorised it."""
    from support_agent.contracts import ModelResponse

    async with (
        connect(server, requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools) as waits,
    ):
        approvals = Recording(waits.approvals)
        agent = refund_agent(
            tools,
            approvals,
            ScriptedClient([wants_refund(order, **stated), ModelResponse(text=ISSUED)]),
        )
        result, _ = await agent.handle(f"please refund {order}", identity=customer())
        (asked,) = approvals.asked
        row = await waits.approvals.get(asked)

    assert type(result).__name__ == outcome, result
    assert server.state["refunded"] == refunded
    assert row is not None and row.args["amount"] == amount, "the amount is the order's total"
    assert row.decided_by == granted_by
    if refunded:
        assert result.reply == ISSUED, "a refund that happened may be said to have happened"
