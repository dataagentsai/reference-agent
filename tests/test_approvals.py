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


# (status, may a refund be requested at all) — AOAS request_refund.preconditions
REQUESTABLE_CASES = [
    ("pending", False),
    ("confirmed", False),
    ("picked", False),
    ("shipped", False),
    ("out_for_delivery", False),
    ("delivered", True),
    ("cancelled", True),
    ("returned", True),
    ("refunded", False),
]


@pytest.mark.parametrize(
    ("status", "requestable"), REQUESTABLE_CASES, ids=[c[0] for c in REQUESTABLE_CASES]
)
@pytest.mark.discharges("P-REFUND", "op:request_refund")
def test_which_orders_a_refund_may_be_requested_for(status: str, requestable: bool) -> None:
    """Before delivery the answer is a cancellation, not a reviewer's time: a note
    planted in a shipped order must not be able to put a refund request in a
    person's queue (generation run 1). A refused request says what to do instead."""
    reason = ap.not_requestable({"status": status, "total": 500}, ap.Policy())
    assert (reason is None) is requestable
    if reason is not None:
        assert status in reason and "cancel" in reason


# --------------------------------------------------------------------------- #
# What a decision was decided against — F-054.
#
# Every other control on this path asks about the decision: who made it, when,
# whether it is still inside its window. None of them asks whether the thing it
# was made about is still the same thing. That is the one an hour-long wait
# needs, because the expiry is satisfied by a grant made a minute ago and the
# row can move in a minute.
# --------------------------------------------------------------------------- #

# (why, the order as it read when assessed, what is recorded)
JUDGED_CASES = [
    ("what the decision rests on, and as strings",
     {"status": "returned", "total": 12400}, {"status": "returned", "total": "12400"}),
    ("a total nobody could read is still recorded, so a later None is a change",
     {"status": "returned"}, {"status": "returned", "total": "None"}),
    ("what moves on its own is left out, or midnight would expire every grant",
     {"status": "returned", "total": 12400, "days_since_delivery": 3},
     {"status": "returned", "total": "12400"}),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "order", "recorded"), JUDGED_CASES, ids=[c[0] for c in JUDGED_CASES]
)
@pytest.mark.discharges("AAC-0078", "AHC-0057")
def test_what_a_refund_decision_is_recorded_against(why: str, order: dict, recorded: dict) -> None:
    """The recorded fields are the judged fields and nothing else.

    Recording more is not free caution: a field that changes by itself makes
    the check cry wolf, and a control that cries wolf is switched off.
    """
    assert ap.judged(order) == recorded


WAS = {"status": "returned", "total": "12400"}

# (why, what was recorded, what the order says now, what has moved)
MOVED_CASES = [
    ("nothing has moved", WAS, {"status": "returned", "total": "12400"}, None),
    ("the total moved — the money case", WAS, {"status": "returned", "total": "41000"},
     "total was '12400' and is now '41000'"),
    ("somebody else refunded it meanwhile", WAS, {"status": "refunded", "total": "12400"},
     "status was 'returned' and is now 'refunded'"),
    ("a judged field can no longer be read", WAS, {"status": "returned"},
     "total was '12400' and is now None"),
    ("both, and a person is told both", WAS, {"status": "cancelled", "total": "0"},
     "status was 'returned' and is now 'cancelled', total was '12400' and is now '0'"),
    ("fields nobody judged do not invalidate a grant", WAS,
     {"status": "returned", "total": "12400", "days_since_delivery": "40"}, None),
    ("nothing was recorded, so nothing can be compared", {},
     {"status": "cancelled", "total": "41000"}, None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "was", "now", "changed"), MOVED_CASES, ids=[c[0] for c in MOVED_CASES]
)
@pytest.mark.discharges("AAC-0078", "AHC-0057")
def test_what_has_moved_since_a_decision_was_made(
    why: str, was: dict, now: dict, changed: str | None
) -> None:
    """The comparison, in words a person can act on.

    The last row is the honest limit of this control rather than a hole in it:
    an approval with nothing recorded is one nobody judged any facts for, and
    inventing a refusal for it would fail every approval raised before this
    field existed. What keeps it from mattering is the row above it in
    `JUDGED_CASES` — the assessment always records.
    """
    assert ap.moved(record(decided_against=was), now) == changed


@pytest.mark.discharges("AAC-0078", "AAC-0053")
async def test_a_blip_on_the_recheck_is_not_news_about_the_order() -> None:
    """A read that fails for a protocol reason raises, so the activity retries.

    The distinction this rests on is the one the tool boundary already draws.
    An *execution* error is the far end speaking about the order — no such
    order, not this customer's — and is the strongest evidence the facts moved.
    A *protocol* error is the far end not speaking at all, and says nothing
    about the order. Answering "stale" to silence would make one dropped
    connection destroy a decision a person had already made.

    Reaching for the private method deliberately: the public path is a Temporal
    activity that wants an activity context, and what is being pinned here is
    the branch, not the plumbing around it.
    """
    from support_agent.approvals.refund import RefundWork
    from support_agent.contracts import ToolUnavailable

    class Unreachable:
        async def list_tools(self, identity: Identity):
            raise ToolUnavailable("the order system is not answering")

        async def call(self, *args: object, **kwargs: object):
            raise AssertionError("nothing may be called when the order cannot be read")

    async def acting_for(customer_id: str) -> Identity:
        return customer()

    work = RefundWork(tools=Unreachable(), acting_for=acting_for)  # type: ignore[arg-type]
    with pytest.raises(ToolUnavailable):
        await work._changed(granted(decided_against=WAS), customer())


# (the order as the order system holds it, refused before any person sees it)
ASSESS_CASES = [
    ({"id": "O-1", "status": "shipped", "total": 500}, True),
    ({"id": "O-1", "status": "pending", "total": 500}, True),
    ({"id": "O-1", "status": "delivered", "total": 500}, False),
    ({"id": "O-1", "status": "returned", "total": 99999}, False),
]


@pytest.mark.parametrize(
    ("order", "refused"), ASSESS_CASES, ids=[c[0]["status"] for c in ASSESS_CASES]
)
@pytest.mark.discharges("op:request_refund", "AAC-0056")
async def test_an_ineligible_request_is_refused_before_it_reaches_a_person(
    order: dict, refused: bool
) -> None:
    """The assessment reads the order and refuses a request the AOAS does not
    allow, as a result the model is told — no approval waits, nobody is asked.
    An eligible one goes on to the automatic limit as before."""
    from support_agent.approvals.durable import Ask
    from support_agent.approvals.refund import RefundWork
    from support_agent.contracts import ToolResult, ToolSpec

    spec = ToolSpec(
        name="get_order",
        description="read an order",
        input_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
        output_schema={"type": "object"},
        side_effect=SideEffectClass.READ,
    )

    class OrderSystem:
        async def list_tools(self, identity: Identity):
            return {"get_order": spec}

        async def call(self, name: str, arguments: dict, identity: Identity, key: object):
            return ToolResult(name=name, text="", structured=order)

    async def acting_for(customer_id: str) -> Identity:
        return customer()

    work = RefundWork(tools=OrderSystem(), acting_for=acting_for)  # type: ignore[arg-type]
    ask = Ask(
        id="A-1",
        action=ap.REFUND_ACTION,
        args={"order_id": "O-1"},
        customer_id="C-1042",
        idempotency_key="run-1:3:0",
        ttl_s=3600,
        queue="q",
    )
    assessment = await work.assess(ask)
    assert (assessment.failed is not None) is refused
    if refused:
        assert order["status"] in assessment.failed and assessment.reason is None


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
    # Its own copy of the orders, because F-054 is only reachable in a world
    # that can move: a test has to change one *between* the decision and the
    # effect, and a shared dict would leak that change into every other test.
    srv.state = {"refunds": 0, "refunded": [], "orders": dict(ORDERS)}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return srv.state["orders"][order_id]  # type: ignore[attr-defined]

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


# (why, what the colleague does, seconds before they do, what the order does
#  while they think, refunds before the customer speaks again, what the next
#  turn is, what it says)
ACROSS_TURNS = [
    ("granted: refunded before the customer asks, and told so", True, HOUR, None, 1,
     "Completed", "refund is on its way"),
    ("refused: nothing refunded, and told so", False, HOUR, None, 0,
     "Completed", "could not authorise"),
    ("nobody yet: still with a colleague", None, HOUR, None, 0,
     "Completed", "still with a colleague"),
    ("nobody in time: the authorisation lapsed", None, DAY + 300, None, 0,
     "Failed", "no longer valid"),
    ("the total moved under them: granted, and no money leaves", True, HOUR, {"total": 41000}, 0,
     "Failed", "changed while a colleague was reviewing"),
    ("refunded by someone else meanwhile: granted, and not again", True, HOUR,
     {"status": "refunded"}, 0, "Failed", "changed while a colleague was reviewing"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "grant", "later", "moves", "refunds", "outcome", "says"),
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
    server,
    why: str,
    grant: bool | None,
    later: int,
    moves: dict[str, object] | None,
    refunds: int,
    outcome: str,
    says: str,
) -> None:
    """The whole gate, through the drivable surface.

    Turn one asks and is told nothing has been refunded. A colleague decides,
    or does not, and a granted refund is issued then, not when the customer
    next speaks. Turn two only reports it, without the model.

    The last two rows are F-054. Everything about the decision is correct — a
    live grant, the right reviewer, inside the window, the original key — and
    the order is no longer the order that was judged. A grant is permission to
    do a particular thing to a particular row, so when the row moves the
    permission is spent on nothing and the customer is asked to ask again.
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
        if moves is not None:
            order = server.state["orders"]["AB-201"]
            server.state["orders"]["AB-201"] = order.model_copy(update=moves)
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
