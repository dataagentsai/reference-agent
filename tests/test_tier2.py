"""Tier 2 — the escalations nobody asked for.

Every rule tested here fires without the customer writing a word that mentions a
person. That is the whole distinction: Tier 1 answers *what did they say*, Tier 2
answers *how is this going*.

Two of these matter more than the rest. `loop-exhausted` closes R-011 — a
trajectory that gave up could not previously fetch a person, because escalation
existed only ahead of the loop. And the cooldown is what makes the tier safe to
ship at all: a Tier 2 condition does not stop holding because an escalation
lapsed, so without it one failing conversation would mint reference numbers
forever.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import (
    Completed,
    Escalated,
    Identity,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    Usage,
)
from support_agent.escalation import rules as t2
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import Conversation, InMemoryCheckpointStore, TurnNote
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


def says(text: str) -> ModelResponse:
    return ModelResponse(text=text, usage=Usage(input_tokens=5, output_tokens=2))


# --------------------------------------------------------------------------- #
# The rules, as pure functions. No agent, no store, no clock.
# --------------------------------------------------------------------------- #

FIRES = [
    ("the loop gave up", t2.Facts(termination="step_budget_exhausted"), "loop-exhausted"),
    ("cost ceiling", t2.Facts(termination="cost_ceiling_reached"), "loop-exhausted"),
    ("going in circles", t2.Facts(termination="oscillation_detected"), "loop-exhausted"),
    ("tools are down", t2.Facts(consecutive_failed=2), "tool-unavailable"),
    ("asked three times", t2.Facts(repeated_intent=3), "repeated-intent"),
    ("refused twice", t2.Facts(refusals=2), "second-refusal"),
    ("a very long conversation", t2.Facts(turn_count=10), "turns-exceeded"),
]


@pytest.mark.parametrize(("name", "facts", "rule_id"), FIRES, ids=[c[0] for c in FIRES])
def test_what_earns_a_person(name: str, facts: t2.Facts, rule_id: str) -> None:
    matched = t2.evaluate(facts)
    assert matched is not None, name
    assert matched.id == rule_id


QUIET = [
    ("a normal turn", t2.Facts(turn_count=1)),
    ("one failure is not a pattern", t2.Facts(consecutive_failed=1)),
    ("one refusal is the agent working", t2.Facts(refusals=1)),
    ("asked twice", t2.Facts(repeated_intent=2)),
    ("the loop finished", t2.Facts(termination="goal_reached")),
    ("waiting on a human already", t2.Facts(termination="awaiting_approval")),
]


@pytest.mark.parametrize(("name", "facts"), QUIET, ids=[c[0] for c in QUIET])
def test_what_does_not(name: str, facts: t2.Facts) -> None:
    """The over-escalation half. A refusal is the agent working correctly, and
    one tool failure is a retry rather than an outage."""
    assert t2.evaluate(facts) is None, name


def test_the_most_diagnostic_rule_wins() -> None:
    """First match wins, so order in the rule set is versioned configuration.

    A conversation that is long *and* whose loop just gave up should fetch a
    person for the second reason: it is the far better description of what went
    wrong, and it is what the reviewer needs to read first.
    """
    both = t2.Facts(turn_count=20, termination="step_budget_exhausted")
    matched = t2.evaluate(both)
    assert matched is not None and matched.id == "loop-exhausted"


def test_urgency_is_per_rule_not_per_escalation() -> None:
    """A stalled trajectory should not wait as long as a chatty conversation."""
    by_id = {r.id: r for r in t2.DEFAULT_RULES}
    assert by_id["loop-exhausted"].priority < by_id["turns-exceeded"].priority
    assert by_id["loop-exhausted"].ttl_s < by_id["turns-exceeded"].ttl_s


# --------------------------------------------------------------------------- #
# The cooldown. Without it, one bad conversation mints references forever.
# --------------------------------------------------------------------------- #


def test_a_rule_fires_once_per_conversation() -> None:
    fired = t2.Facts(
        consecutive_failed=3, escalations=1, already_fired=frozenset({"tool-unavailable"})
    )
    assert t2.evaluate(fired) is None, "the condition still holds; the rule has had its turn"


def test_a_second_rule_may_still_fire() -> None:
    """The cooldown is per rule, not a gag. A conversation whose tools failed and
    which then ran very long has two different things worth telling a person."""
    facts = t2.Facts(
        consecutive_failed=3,
        turn_count=12,
        escalations=1,
        already_fired=frozenset({"tool-unavailable"}),
    )
    matched = t2.evaluate(facts)
    assert matched is not None and matched.id == "turns-exceeded"


def test_the_conversation_cap_stops_everything() -> None:
    """Past the cap the agent stops promising. A third reference number for one
    unresolved problem helps nobody and makes the queue read as three customers."""
    facts = t2.Facts(consecutive_failed=3, turn_count=20, escalations=t2.MAX_PER_CONVERSATION)
    assert t2.evaluate(facts) is None


# --------------------------------------------------------------------------- #
# Through the agent. The facts have to be assembled from real state.
# --------------------------------------------------------------------------- #


def agent_with(tools, *, escalations, llm=None) -> ep.Agent:
    return ep.build(
        llm=llm or ScriptedClient([says("ok")] * 20),
        tools=tools,
        store=InMemoryCheckpointStore(),
        escalations=escalations,
    )


async def test_a_repeatedly_refused_customer_reaches_a_person(server) -> None:
    """Nobody asked for one. The agent refused twice, which is the agent working
    correctly *and* a sign that a person should decide."""
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        first, conversation = await agent.handle("can I get a discount?", identity=customer())
        second, conversation = await agent.handle(
            "what about a coupon then?", identity=customer(), conversation=conversation
        )

    assert first.kind == "refused", "the first refusal is just a refusal"
    assert isinstance(second, Escalated)
    assert second.rule_id == "second-refusal"
    assert second.ticket_id is not None

    raised = await store.get(second.ticket_id)
    assert raised is not None and raised.tier == 2
    assert conversation.escalated_rules == ("second-refusal",)


async def test_the_same_rule_does_not_raise_again_after_it_lapses(server) -> None:
    """The loop the cooldown exists to prevent.

    The escalation lapses, the conversation comes back to the agent, and the
    customer is refused again — the condition still holds. Without the cooldown
    this mints a second reference, and a third, for as long as they keep talking.
    """
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        await agent.handle("can I get a discount?", identity=customer())
        _, conversation = await agent.handle("can I get a discount?", identity=customer())
        _, conversation = await agent.handle(
            "a coupon?", identity=customer(), conversation=conversation
        )

        # Nobody came.
        open_now = (await store.pending())[0]
        await store.put(open_now.model_copy(update={"expires_at": 0}))

        handed_back, conversation = await agent.handle(
            "hello?", identity=customer(), conversation=conversation
        )
        again, conversation = await agent.handle(
            "so about that discount", identity=customer(), conversation=conversation
        )

    assert isinstance(handed_back, Completed), "lapsed, so the agent has it again"
    assert not isinstance(again, Escalated), "the same rule must not fetch a second person"
    assert len(await store.pending()) == 0


async def test_a_tier_one_escalation_is_not_overridden(server) -> None:
    """A turn that already fetched a person does not need a second reason to."""
    store = esc.InMemoryEscalationStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=store)
        conversation = Conversation(
            conversation_id="cnv_x",  # type: ignore[arg-type]
            customer_id="C-1042",
            turn_count=40,
            recent=(TurnNote(route="agentic", result="refused"),) * 3,
        )
        result, _ = await agent.handle(
            "put me through to a human", identity=customer(), conversation=conversation
        )

    assert isinstance(result, Escalated)
    assert result.rule_id == "asked-for-human", "tier 1 decided, and tier 2 left it alone"
    assert len(await store.pending()) == 1, "one person fetched, not two"


async def test_without_a_store_tier_two_never_fires(server) -> None:
    """Same honesty as everywhere else: an agent with nowhere to write cannot
    escalate, and does not pretend to."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([says("ok")] * 5),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        _, conversation = await agent.handle("can I get a discount?", identity=customer())
        second, conversation = await agent.handle(
            "a coupon?", identity=customer(), conversation=conversation
        )

    assert second.kind == "refused"
    assert conversation.pending_escalation_id is None


# --------------------------------------------------------------------------- #
# What the conversation now remembers, and what it refuses to.
# --------------------------------------------------------------------------- #


async def test_a_turn_is_recorded_as_facts_not_prose(server) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = agent_with(tools, escalations=esc.InMemoryEscalationStore())
        _, conversation = await agent.handle("where is my order AB-12345", identity=customer())

    assert conversation.turn_count == 1
    note = conversation.recent[-1]
    assert (note.route, note.result, note.intent) == ("direct", "completed", "order_status")


def test_the_remembered_history_is_bounded() -> None:
    """A checkpoint is written and read every turn. An unbounded history is how a
    conversation that runs all day becomes a row nobody can load."""
    from support_agent.state import RECENT_TURNS

    conversation = Conversation(conversation_id="c", customer_id="C-1")  # type: ignore[arg-type]
    for _ in range(RECENT_TURNS + 30):
        conversation = conversation.with_turn(TurnNote(route="agentic", result="completed"))

    assert len(conversation.recent) == RECENT_TURNS
    assert conversation.turn_count == RECENT_TURNS + 30, "the real count survives the cap"


def test_the_termination_reason_reaches_the_facts() -> None:
    """R-011's fix, at the seam where it happens: the loop records why it stopped
    and a rule reads it, with neither knowing about the other."""
    conversation = Conversation(  # type: ignore[arg-type]
        conversation_id="c",
        customer_id="C-1",
        turn_count=1,
        recent=(
            TurnNote(
                route="agentic",
                result="completed",
                termination=TerminationReason.STEP_BUDGET_EXHAUSTED.value,
            ),
        ),
    )
    facts = ep._facts(conversation)
    assert facts.termination == "step_budget_exhausted"
    matched = t2.evaluate(facts)
    assert matched is not None and matched.id == "loop-exhausted"
