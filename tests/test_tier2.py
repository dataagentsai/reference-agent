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
from evals import durable
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import (
    Escalated,
    Identity,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    Usage,
)
from support_agent.escalation import rules as t2
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
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
@pytest.mark.discharges(
    "esc:loop-exhausted",
    "esc:tool-unavailable",
    "esc:repeated-intent",
    "esc:second-refusal",
    "esc:turns-exceeded",
    "AAC-0043",
)
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
@pytest.mark.discharges(
    "esc:loop-exhausted",
    "esc:tool-unavailable",
    "esc:repeated-intent",
    "esc:second-refusal",
    "esc:turns-exceeded",
    "AAC-0043",
)
def test_what_does_not(name: str, facts: t2.Facts) -> None:
    """The over-escalation half. A refusal is the agent working correctly, and
    one tool failure is a retry rather than an outage."""
    assert t2.evaluate(facts) is None, name


@pytest.mark.discharges("esc:loop-exhausted", "esc:turns-exceeded")
def test_the_most_diagnostic_rule_wins() -> None:
    """First match wins, so order in the rule set is versioned configuration.

    A conversation that is long *and* whose loop just gave up should fetch a
    person for the second reason: it is the far better description of what went
    wrong, and it is what the reviewer needs to read first.
    """
    both = t2.Facts(turn_count=20, termination="step_budget_exhausted")
    matched = t2.evaluate(both)
    assert matched is not None and matched.id == "loop-exhausted"


@pytest.mark.discharges("esc:loop-exhausted", "esc:turns-exceeded")
def test_urgency_is_per_rule_not_per_escalation() -> None:
    """A stalled trajectory should not wait as long as a chatty conversation."""
    by_id = {r.id: r for r in t2.DEFAULT_RULES}
    assert by_id["loop-exhausted"].priority < by_id["turns-exceeded"].priority
    assert by_id["loop-exhausted"].ttl_s < by_id["turns-exceeded"].ttl_s


# --------------------------------------------------------------------------- #
# The cooldown. Without it, one bad conversation mints references forever.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("P-ESC-ONCE")
def test_a_rule_fires_once_per_conversation() -> None:
    fired = t2.Facts(
        consecutive_failed=3, escalations=1, already_fired=frozenset({"tool-unavailable"})
    )
    assert t2.evaluate(fired) is None, "the condition still holds; the rule has had its turn"


@pytest.mark.discharges("P-ESC-ONCE", "P-ESC-CAP")
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


@pytest.mark.discharges("P-ESC-CAP")
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


@pytest.mark.discharges("esc:second-refusal", "op:escalate", "AAC-0043")
async def test_a_repeatedly_refused_customer_reaches_a_person(server) -> None:
    """Nobody asked for one. The agent refused twice, which is the agent working
    correctly *and* a sign that a person should decide."""
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
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


@pytest.mark.discharges("P-ESC-ONCE", "P-ESC-TTL")
async def test_the_same_rule_does_not_raise_again_while_the_agent_holds_it(server) -> None:
    """The loop the cooldown exists to prevent, within one stretch.

    Refused twice and handed over; the customer goes on being refused while the
    agent holds the conversation. The condition still holds — it would hold for
    as long as they kept talking — and the rule has had its turn.
    """
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = agent_with(tools, escalations=store)
        _, conversation = await agent.handle("can I get a discount?", identity=customer())
        _, conversation = await agent.handle(
            "a coupon?", identity=customer(), conversation=conversation
        )
        facts = t2.facts_of(conversation)

    assert facts.refusals == 2 and "second-refusal" in facts.already_fired
    assert t2.evaluate(facts) is None


def hand_back(store: durable.RememberedEscalations, how: str, escalation_id: str) -> None:
    if how == "resolved":
        store.resolved(escalation_id, by="desk-1")
    else:
        store.lapse(escalation_id)


# (how the conversation came back to the agent)
HANDBACKS = ["resolved", "lapsed"]


@pytest.mark.parametrize("how", HANDBACKS)
@pytest.mark.discharges(
    "P-ESC-FRESH", "P-ESC-ONCE", "P-ESC-CAP", "P-ESC-LAPSE", "esc:second-refusal"
)
async def test_a_handback_starts_the_count_again_and_the_cap_still_holds(server, how: str) -> None:
    """T-076c, decided by the owner on 2026-10-01.

    After a colleague hands the conversation back — or nobody came and it
    lapsed — what happens next is new evidence. One refusal after the return is
    one, not three: the two before it were seen by the person who had it
    (P-ESC-FRESH). A second refusal fetches a person again, because the rule
    fires once per stretch the agent holds the conversation (P-ESC-ONCE). And
    after a second handback, two more refusals fetch nobody: the cap of two is
    per conversation and is never reset (P-ESC-CAP).
    """
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = agent_with(tools, escalations=store)

        async def turn(text: str, conversation: Conversation | None):
            return await agent.handle(text, identity=customer(), conversation=conversation)

        _, conversation = await turn("can I get a discount?", None)
        first, conversation = await turn("a coupon?", conversation)
        assert isinstance(first, Escalated) and first.rule_id == "second-refusal"

        hand_back(store, how, (await store.pending())[0].id)
        _, conversation = await turn("hello?", conversation)
        assert conversation.pending_escalation_id is None, "back with the agent"

        once, conversation = await turn("can I get a discount?", conversation)
        assert once.kind == "refused", "the refusals before the handback do not count"
        assert t2.facts_of(conversation).refusals == 1
        assert conversation.turn_count >= 3, "the conversation's own count is untouched"

        again, conversation = await turn("a voucher, then?", conversation)
        assert isinstance(again, Escalated) and again.rule_id == "second-refusal"
        assert again.ticket_id != first.ticket_id

        hand_back(store, how, (await store.pending())[0].id)
        _, conversation = await turn("hello?", conversation)
        _, conversation = await turn("can I get a discount?", conversation)
        third, conversation = await turn("a coupon?", conversation)

    assert not isinstance(third, Escalated), "past the cap no third reference"
    assert conversation.escalations_raised == t2.MAX_PER_CONVERSATION
    assert await store.pending() == ()


def notes(*results: str) -> tuple[TurnNote, ...]:
    return tuple(TurnNote(route="agentic", result=r, intent="order_status") for r in results)


# (name, the remembered turns, turn_count when it came back, the facts expected)
FRESH = [
    (
        "never left: every turn counts",
        notes("failed", "failed", "refused"),
        0,
        {"turn_count": 3, "consecutive_failed": 0, "refusals": 1, "repeated_intent": 3},
    ),
    (
        "failures before the return do not count",
        notes("failed", "failed", "failed"),
        2,
        {"turn_count": 1, "consecutive_failed": 1, "refusals": 0, "repeated_intent": 1},
    ),
    (
        "refusals before the return do not count",
        notes("refused", "refused", "completed", "refused"),
        2,
        {"turn_count": 2, "consecutive_failed": 0, "refusals": 1, "repeated_intent": 2},
    ),
    (
        "just returned: nothing yet",
        notes("failed", "failed"),
        2,
        {"turn_count": 0, "consecutive_failed": 0, "refusals": 0, "repeated_intent": 0},
    ),
]


@pytest.mark.parametrize(
    ("name", "recent", "returned_at", "expected"), FRESH, ids=[f[0] for f in FRESH]
)
@pytest.mark.discharges(
    "P-ESC-FRESH",
    "fact:consecutive_failed",
    "fact:refusals",
    "fact:repeated_intent",
    "fact:turn_count",
)
def test_the_condition_facts_count_from_the_latest_return(
    name: str, recent: tuple[TurnNote, ...], returned_at: int, expected: dict[str, int]
) -> None:
    conversation = Conversation(
        conversation_id="cnv_f",  # type: ignore[arg-type]
        customer_id="C-1042",
        recent=recent,
        turn_count=len(recent),
        returned_at_turn=returned_at,
    )
    facts = t2.facts_of(conversation)
    assert {k: facts.get(k) for k in expected} == expected
    # Every other reader keeps the whole history.
    assert conversation.turn_count == len(recent) and conversation.recent == recent


@pytest.mark.discharges("P-ESC-ONCE", "P-ESC-FRESH", "P-ESC-CAP")
def test_returning_resets_the_cooldown_and_never_the_cap() -> None:
    held = Conversation(
        conversation_id="cnv_r",  # type: ignore[arg-type]
        customer_id="C-1042",
        turn_count=7,
        escalated_rules=("tool-unavailable",),
        escalations_raised=1,
        pending_escalation_id="E-1",
    )
    back = held.returned()
    assert back.pending_escalation_id is None
    assert back.escalated_rules == ()
    assert back.returned_at_turn == 7
    assert back.escalations_raised == 1, "the cap is what stops a loop"


@pytest.mark.discharges("P-ESC-TIER1")
async def test_a_tier_one_escalation_is_not_overridden(server) -> None:
    """A turn that already fetched a person does not need a second reason to."""
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
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


@pytest.mark.discharges("AAC-0110", "op:escalate")
async def test_without_a_store_tier_two_never_fires(server) -> None:
    """Same honesty as everywhere else: an agent with nowhere to write cannot
    escalate, and does not pretend to."""
    async with connect(server, requests=InMemoryRequests()) as tools:
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


@pytest.mark.discharges("fact:turn_count", "fact:repeated_intent")
async def test_a_turn_is_recorded_as_facts_not_prose(server) -> None:
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = agent_with(tools, escalations=durable.RememberedEscalations())
        _, conversation = await agent.handle("where is my order AB-12345", identity=customer())

    assert conversation.turn_count == 1
    note = conversation.recent[-1]
    assert (note.route, note.result, note.intent) == ("direct", "completed", "order_status")


@pytest.mark.discharges("AHC-0066")
def test_the_remembered_history_is_bounded() -> None:
    """A checkpoint is written and read every turn. An unbounded history is how a
    conversation that runs all day becomes a row nobody can load."""
    from support_agent.state import RECENT_TURNS

    conversation = Conversation(conversation_id="c", customer_id="C-1")  # type: ignore[arg-type]
    for _ in range(RECENT_TURNS + 30):
        conversation = conversation.with_turn(TurnNote(route="agentic", result="completed"))

    assert len(conversation.recent) == RECENT_TURNS
    assert conversation.turn_count == RECENT_TURNS + 30, "the real count survives the cap"


@pytest.mark.discharges("esc:loop-exhausted", "AAC-0043")
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
    facts = t2.facts_of(conversation)
    assert facts.termination == "step_budget_exhausted"
    matched = t2.evaluate(facts)
    assert matched is not None and matched.id == "loop-exhausted"


@pytest.mark.discharges("esc:repeated-intent", "op:escalate", "AAC-0043")
async def test_a_customer_asking_the_same_thing_three_times_reaches_a_person(server) -> None:
    """F-025: the rule existed and could not fire.

    Nobody quotes an order id, so every turn goes to the loop — and the router
    still classified each one as the same intent. The fact counted only turns
    that ended unresolved, and a turn the agent answered reset it, so the third
    ask never reached the threshold it was written for.
    """
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = agent_with(tools, escalations=store)
        conversation = None
        results = []
        for _ in range(3):
            result, conversation = await agent.handle(
                "I want to cancel my order", identity=customer(), conversation=conversation
            )
            results.append(result)

    first, second, third = results
    assert first.kind == "completed" and second.kind == "completed", "asking twice is just asking"
    assert isinstance(third, Escalated), third
    assert third.rule_id == "repeated-intent"
    assert conversation.escalated_rules == ("repeated-intent",)


@pytest.mark.discharges("P-ESC-CAP", "esc:asked-for-human", "op:escalate")
async def test_the_cap_holds_however_the_escalation_was_raised(server) -> None:
    """F-034, found by a live twelve-turn run: three handoffs in a conversation
    whose cap is two.

    The cap lived in the Tier 2 evaluator, and a Tier 1 escalation — decided from
    the turn's own words — went straight past it. So a customer who kept asking
    for a person got a new reference every time they asked, each one reading like
    progress and none of it being any.
    """
    store = durable.RememberedEscalations()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = agent_with(tools, escalations=store)
        conversation = None
        results = []
        for _ in range(4):
            # Resolved between turns, so the next ask is not merely held by the
            # one before it — the cap is the thing under test, not the hold.
            result, conversation = await agent.handle(
                "I want to speak to a human", identity=customer(), conversation=conversation
            )
            results.append(result)
            # A colleague closes each one, as the desk would.
            for open_one in await store.pending():
                store.resolved(open_one.id, by="desk-1")

    raised = [r for r in results if isinstance(r, Escalated)]
    assert len(raised) == 2, f"the cap is 2 and {len(raised)} references were issued"
    assert all(r.kind == "completed" for r in results[2:]), results[2:]
    assert esc.CAPPED_REPLY in results[-1].reply


# (name, the intent each of three turns was classified as, the count it makes)
# The AOAS (owner decision, 2026-09-26): only a named intent repeated counts, so
# small talk the router classifies as nothing never fetches a person.
REPEATS = [
    ("the same question three times", ["order_status"] * 3, 3),
    ("three messages nothing classified", [None, None, None], 0),
    ("a named intent after small talk", [None, None, "order_status"], 1),
    ("a change of subject resets it", ["order_status", "order_status", "cancel_order"], 1),
]


@pytest.mark.parametrize(("name", "intents", "count"), REPEATS, ids=[r[0] for r in REPEATS])
@pytest.mark.discharges("fact:repeated_intent", "esc:repeated-intent")
def test_only_a_named_intent_counts_as_repeated(name: str, intents: list, count: int) -> None:
    notes = tuple(TurnNote(route="agentic", result="completed", intent=i) for i in intents)
    conversation = Conversation(conversation_id="cnv_t", customer_id="C-1042", recent=notes)
    assert t2.facts_of(conversation).repeated_intent == count
