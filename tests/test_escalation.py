"""Handing a conversation to a person, and what survives the handoff.

Steps 1–3 of the escalation rebuild. Before them, an escalation was a sentence:
the agent told a customer a colleague would take over, wrote nothing down, and
answered their next message itself.

Three properties are worth more than the rest, and each has a table:

- **the rule fires on the request, not on a noun** — the over-escalation guard;
- **the record exists before the customer is told** — `ticket_id` is a real id;
- **the conversation is held while a person owns it** — and handed back if
  nobody comes, which is what makes this shippable before the reviewer surface.
"""

from __future__ import annotations

import time

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, TypeAdapter

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import router
from support_agent import telemetry as tel
from support_agent.contracts import (
    Completed,
    Escalate,
    Escalated,
    EscalationState,
    Identity,
    Refused,
    SideEffectClass,
    TerminationReason,
    TurnResult,
)
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import META_SIDE_EFFECT, connect


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@pytest.fixture
def server():
    srv = MCPServer("ecom")

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def agent_with(tools, *, escalations=None, llm=None) -> ep.Agent:
    """A scripted client with nothing in it: reaching the model raises."""
    return ep.build(
        llm=llm or ScriptedClient([]),
        tools=tools,
        store=InMemoryCheckpointStore(),
        escalations=escalations,
    )


# --------------------------------------------------------------------------- #
# The rule. Anchored on the request, not on a noun that happens to appear.
# --------------------------------------------------------------------------- #

ASKS_FOR_A_HUMAN = [
    ("plain request", "just put me through to a human"),
    ("no article", "I want to talk to human"),
    ("mid-sentence, after an intent", "cancel AB-12345, actually get me a manager"),
    ("indirect", "can I speak to someone"),
    ("states a need", "I need a supervisor"),
    ("real person", "get me a real person"),
    ("says the word", "please escalate this"),
    ("speak with", "I want to speak with an agent"),
    ("connect", "connect me to a representative"),
    ("bare transfer", "transfer me"),
    ("wants one", "I want a human"),
    ("chat with", "chat with a person"),
]


@pytest.mark.parametrize(("name", "text"), ASKS_FOR_A_HUMAN, ids=[c[0] for c in ASKS_FOR_A_HUMAN])
@pytest.mark.discharges("esc:asked-for-human", "AAC-0043")
def test_a_request_for_a_person_escalates(name: str, text: str) -> None:
    decision = router.route(text)
    assert decision.kind == "escalate", text
    assert decision.rule_id == "asked-for-human"


MENTIONS_A_PERSON_WITHOUT_ASKING = [
    ("the courier", "the delivery agent left it at the wrong door"),
    ("quoting support", "your support agent said 5 days"),
    ("a shop manager", "the manager of the store signed for it"),
    ("intent plus a noun", "I want to cancel my order, the delivery agent took it back"),
    ("a coupon code", "my rep code is AGENT50"),
    ("asking about review", "is a human reviewing my refund"),
]


@pytest.mark.parametrize(
    ("name", "text"),
    MENTIONS_A_PERSON_WITHOUT_ASKING,
    ids=[c[0] for c in MENTIONS_A_PERSON_WITHOUT_ASKING],
)
@pytest.mark.discharges("esc:asked-for-human", "AAC-0043")
def test_naming_a_person_is_not_asking_for_one(name: str, text: str) -> None:
    """Over-escalation, and the reason it is hard to see: the route was recorded
    as correct, because the rule really did fire. Every line here escalated
    before the pattern was anchored on the request."""
    assert router.route(text).kind != "escalate", text


@pytest.mark.discharges("esc:lost-in-transit", "AAC-0043")
def test_the_policy_rule_still_fires_without_anyone_asking() -> None:
    """The second rule is a different kind of thing — nobody requested a human,
    the case class simply is not the agent's to resolve."""
    decision = router.route("the courier says it is lost in transit")
    assert decision.kind == "escalate"
    assert decision.rule_id == "lost-in-transit"


# --------------------------------------------------------------------------- #
# The record. Written before the customer is told anything.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("op:escalate", "ext:escalation_desk", "AHC-0070", "AAC-0110")
async def test_an_escalation_writes_a_record_and_names_it(server) -> None:
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        result, conversation = await agent.handle("put me through to a human", identity=customer())

    assert isinstance(result, Escalated)
    assert result.ticket_id is not None, "the field existed for months and was never set"
    assert result.ticket_id in result.reply, "a reference the customer is never told is not one"
    assert result.termination is TerminationReason.AWAITING_HUMAN

    raised = await store.get(result.ticket_id)
    assert raised is not None
    assert raised.state is EscalationState.QUEUED
    assert raised.conversation_id == conversation.conversation_id
    assert raised.customer_id == "C-1042"
    assert raised.rule_id == "asked-for-human"
    assert raised.rules_version == router.Rules().version
    assert (await store.pending()) == (raised,)


async def test_without_a_store_the_handover_is_refused_not_claimed(server) -> None:
    """F-024, and AOAS `escalate.on_refusal`. An agent with nowhere to write
    cannot hand the conversation to anybody — so it says so and stays with the
    request. It used to say "let me pass you to a colleague" and return
    `Escalated` with no ticket: a claimed action with no record, which AAC-0110
    forbids. `Escalated` now requires a ticket, so this cannot be written again.
    """
    async with connect(server, ledger=InMemoryLedger()) as tools:
        result, conversation = await agent_with(tools).handle(
            "put me through to a human", identity=customer()
        )

    assert isinstance(result, Refused), result
    assert "colleague" not in result.reply or "cannot" in result.reply
    assert "pass you to a colleague" not in result.reply, "claimed a handover that did not happen"
    assert result.reply == esc.NO_DESK_REPLY
    assert conversation.pending_escalation_id is None, "nothing to point the flag at"


@pytest.mark.discharges("AHC-0070", "esc:asked-for-human")
async def test_escalation_still_never_reaches_the_model(server) -> None:
    llm = ScriptedClient([])
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=esc.InMemoryEscalationStore(), llm=llm)
        result, _ = await agent.handle("get me a manager", identity=customer())

    assert isinstance(result, Escalated)
    assert llm.calls == [], "writing a record must not cost a model call"


# --------------------------------------------------------------------------- #
# Stickiness. The defect this whole step exists to close.
# --------------------------------------------------------------------------- #

FOLLOW_UPS = [
    ("an unrelated question", "where is my order AB-12345"),
    ("asking again", "hello? is anyone there"),
    ("a new intent", "actually I want to return CD-99999"),
]


@pytest.mark.parametrize(("name", "text"), FOLLOW_UPS, ids=[c[0] for c in FOLLOW_UPS])
@pytest.mark.discharges("op:escalate")
async def test_the_agent_does_not_answer_over_a_live_handoff(server, name: str, text: str) -> None:
    """It used to. The customer was told a colleague would take over and the
    next message was served by the agent as though nothing had happened."""
    store = esc.InMemoryEscalationStore()
    llm = ScriptedClient([])
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store, llm=llm)
        first, conversation = await agent.handle("put me through to a human", identity=customer())
        second, conversation = await agent.handle(
            text, identity=customer(), conversation=conversation
        )

    assert isinstance(first, Escalated)
    assert conversation.pending_escalation_id == first.ticket_id
    assert isinstance(second, Escalated), f"{name} was answered by the agent"
    assert second.ticket_id == first.ticket_id
    assert llm.calls == []


@pytest.mark.discharges("op:escalate")
async def test_a_resolved_escalation_hands_the_conversation_back(server) -> None:
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        first, conversation = await agent.handle("put me through to a human", identity=customer())

        assert first.ticket_id is not None
        raised = await store.get(first.ticket_id)
        assert raised is not None
        await store.put(raised.model_copy(update={"state": EscalationState.RESOLVED}))

        result, conversation = await agent.handle(
            "where is my order AB-12345", identity=customer(), conversation=conversation
        )

    assert isinstance(result, Completed), "a closed escalation must not hold the line"
    assert result.reply == "Order AB-12345 is currently shipped. It is on its way."
    assert conversation.pending_escalation_id is None


@pytest.mark.discharges("op:escalate", "ext:escalation_desk")
async def test_nobody_came_so_the_conversation_is_handed_back(server) -> None:
    """The case that makes step 1 shippable before a reviewer surface exists.

    Without a lapse path, every escalated conversation would be held open
    forever against a queue no human can yet see.
    """
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        first, conversation = await agent.handle("transfer me", identity=customer())

        assert first.ticket_id is not None
        raised = await store.get(first.ticket_id)
        assert raised is not None
        # Wind the clock past the window rather than sleeping through it.
        await store.put(raised.model_copy(update={"expires_at": int(time.time()) - 1}))

        result, conversation = await agent.handle(
            "anyone?", identity=customer(), conversation=conversation
        )

    assert isinstance(result, Completed)
    assert first.ticket_id in result.reply, "say which reference lapsed"
    assert conversation.pending_escalation_id is None

    lapsed = await store.get(first.ticket_id)
    assert lapsed is not None
    assert lapsed.state is EscalationState.EXPIRED, "the record survives as evidence"
    assert (await store.pending()) == (), "and leaves the queue"


# --------------------------------------------------------------------------- #
# Contracts and the edge.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0002", "AHC-0017")
async def test_the_escalated_result_still_round_trips_the_union(server) -> None:
    adapter: TypeAdapter[TurnResult] = TypeAdapter(TurnResult)
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=esc.InMemoryEscalationStore())
        result, _ = await agent.handle("I need a supervisor", identity=customer())

    assert adapter.validate_python(adapter.dump_python(result)) == result


TIER_ONE_SPANS = [
    ("raise", "agent.escalation.raise"),
    ("turn", "agent.turn"),
    ("route", "agent.route"),
]


@pytest.mark.parametrize(("name", "span"), TIER_ONE_SPANS, ids=[c[0] for c in TIER_ONE_SPANS])
@pytest.mark.discharges("B8")
async def test_the_expected_spans_are_emitted(server, exporter, name: str, span: str) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=esc.InMemoryEscalationStore())
        await agent.handle("put me through to a human", identity=customer())

    assert span in {s.name for s in exporter.get_finished_spans()}


@pytest.mark.discharges("B8")
async def test_the_raise_span_carries_the_rule_it_fired(server, exporter) -> None:
    """Required, not optional. A span that says only "escalated" cannot be
    attributed to a rule, and attributing them is the whole mechanism for
    telling over-escalation from correct handoff."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=esc.InMemoryEscalationStore())
        await agent.handle("put me through to a human", identity=customer())

    raised = next(s for s in exporter.get_finished_spans() if s.name == "agent.escalation.raise")
    assert raised.attributes[tel.ESCALATION_RULE] == "asked-for-human"
    assert raised.attributes[tel.ESCALATION_TIER] == 1
    assert raised.attributes[tel.ESCALATION_ID].startswith("E-")


def test_a_conversation_carries_the_flag_across_encoding() -> None:
    """It has to survive the store, or it is not state — it is a local variable
    that happens to be named after one."""
    conversation = Conversation(
        conversation_id="cnv_1",  # type: ignore[arg-type]
        customer_id="C-1042",
        pending_escalation_id="E-DEADBEEF",
    )
    assert Conversation.decode(conversation.encode()).pending_escalation_id == "E-DEADBEEF"


def test_the_router_carries_the_rule_id_into_the_decision() -> None:
    assert Escalate(reason="x").rule_id == "", "default stays empty for hand-built decisions"
    assert router.route("transfer me").tier == 1


# --------------------------------------------------------------------------- #
# Step 6 — never promise a colleague the desk cannot supply.
# --------------------------------------------------------------------------- #

CAPACITY = [
    ("unmeasured desk promises no time", esc.Capacity(), 0, "they will pick this up"),
    ("closed desk says so", esc.Capacity(open=False, per_hour=12), 0, "not available right now"),
    ("open desk gives a real number", esc.Capacity(per_hour=12), 0, "the wait is about"),
]


@pytest.mark.discharges("esc:asked-for-human", "op:escalate")
@pytest.mark.parametrize(
    ("name", "capacity", "depth", "expected"), CAPACITY, ids=[c[0] for c in CAPACITY]
)
async def test_the_reply_says_only_what_the_queue_supports(
    server, name: str, capacity, depth: int, expected: str
) -> None:
    """The defect this closes: "let me pass you to a colleague" with nothing
    behind it, from a system whose own prompt forbids promising what no tool has
    confirmed.

    Both halves are the same rule, settled 2026-09-12: the AOAS permits a wait
    **only** where one is measured — queue depth over observed throughput — and
    forbids one otherwise. Saying nothing when the throughput is unknown is not
    an exception to the rule; it is the rule.
    """
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=esc.InMemoryEscalationStore(),
            capacity=capacity,
        )
        result, _ = await agent.handle("put me through to a human", identity=customer())

    assert isinstance(result, Escalated)
    assert expected in result.reply, result.reply


WAITS = [
    ("a short queue", 12.0, 1, "5 minutes"),
    ("a busy hour", 12.0, 4, "20 minutes"),
    ("a backlog", 12.0, 24, "2 hours"),
    ("nothing measured", None, 4, None),
]


@pytest.mark.parametrize(
    ("name", "per_hour", "depth", "expected"), WAITS, ids=[c[0] for c in WAITS]
)
def test_the_estimate_comes_from_depth_and_throughput(
    name: str, per_hour, depth: int, expected
) -> None:
    """Divided, not guessed. A desk whose rate nobody measured produces no
    estimate at all rather than a plausible one."""
    seconds = esc.Capacity(per_hour=per_hour).estimate_s(depth)
    assert (esc.humanise(seconds) if seconds is not None else None) == expected


@pytest.mark.discharges("op:escalate", "ext:escalation_desk")
async def test_the_sweeper_lapses_what_nobody_came_for(server) -> None:
    """The queue's phantom work.

    Lapse used to run only when the customer sent another turn, so an escalation
    on a conversation somebody abandoned never expired — it sat in the desk's
    queue forever, and the depth every wait estimate divides by drifted upward
    with it.
    """
    store = esc.InMemoryEscalationStore()
    stale = await esc.raise_for(
        store,
        conversation_id="cnv_gone",
        run_id="run_1",
        customer_id="C-1042",
        reason="the customer asked for a human",
        rule_id="asked-for-human",
        rules_version="v1",
        ttl_s=60,
        now=1000,
    )
    fresh = await esc.raise_for(
        store,
        conversation_id="cnv_here",
        run_id="run_2",
        customer_id="C-1042",
        reason="the customer asked for a human",
        rule_id="asked-for-human",
        rules_version="v1",
        ttl_s=3600,
        now=1000,
    )

    lapsed = await esc.sweep(store, now=2000)

    assert [e.id for e in lapsed] == [stale.id], "only what actually expired"
    assert (await store.get(stale.id)).state is EscalationState.EXPIRED
    assert [e.id for e in await store.pending()] == [fresh.id]


async def test_sweeping_twice_lapses_nothing_the_second_time() -> None:
    """It runs on a timer, so it runs against a queue it has already swept."""
    store = esc.InMemoryEscalationStore()
    await esc.raise_for(
        store,
        conversation_id="c",
        run_id="r",
        customer_id="C-1042",
        reason="x",
        rule_id="asked-for-human",
        rules_version="v1",
        ttl_s=60,
        now=1000,
    )
    assert len(await esc.sweep(store, now=2000)) == 1
    assert await esc.sweep(store, now=3000) == ()
