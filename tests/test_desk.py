"""The desk as an actor — F-007, one record along.

`tests/test_escalation.py` drives the escalation queue directly: raise a record,
reach into the store, assert. That exercises the queue. It cannot express a desk
that takes twenty minutes, disagrees that the escalation was needed, walks away,
or arrives after the customer has already been told nobody came — and those are
the four things a real one does.

The scenarios below run the whole path: a customer asks for a person, the agent
raises a record and stops serving, a desk works the queue between turns, and the
customer comes back to find out what happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agenttwin import (
    Clock,
    Desk,
    Live,
    Rule,
    Scenario,
    ScriptedActor,
    StateMachineActor,
    load,
    project,
    run_scenario,
)

from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import EscalationState, Identity, ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
ORDER = "AB-10001"
MINUTE = 60
HOUR = 3600


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def patient() -> ScriptedClient:
    """The agent never reaches the model on an escalated turn, so these exist
    only for the turns after a handoff has been given back."""
    return ScriptedClient([ModelResponse(text="Let me look that up.")] * 6)


async def conversation(desk, *, said=None, turns: int = 3, step_s: int = MINUTE):
    """A customer who asks for a person, then follows up, while a desk does or
    does not pick the escalation up."""
    world = Live.start(load(WORLD))
    scenario = Scenario(name="a customer who wants a person", max_turns=turns)
    actor = ScriptedActor((said or ["I want to speak to a human", "any update?", "hello?"])[:turns])

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=desk.store,
        )
        record = await run_scenario(
            scenario,
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=Clock(step_s=step_s),
        )
    return world, record


def store() -> esc.InMemoryEscalationStore:
    return esc.InMemoryEscalationStore()


# --------------------------------------------------------------------------- #
# The four desks. What each one leaves behind is the point.
# --------------------------------------------------------------------------- #

DESKS = [
    ("picks it up", Desk.answers, MINUTE, "resolved", EscalationState.RESOLVED),
    (
        "says it was not needed",
        Desk.says_agent_could_have,
        MINUTE,
        "agent_could_have",
        EscalationState.RESOLVED,
    ),
    ("never comes", Desk.never_comes, MINUTE, "waiting", EscalationState.QUEUED),
    ("arrives too late", Desk.answers, HOUR, "refused", EscalationState.QUEUED),
]


@pytest.mark.parametrize(
    ("name", "factory", "step_s", "outcome", "state"), DESKS, ids=[c[0] for c in DESKS]
)
@pytest.mark.discharges("ext:escalation_desk", "op:escalate")
async def test_what_each_desk_leaves_behind(
    name: str, factory, step_s: int, outcome: str, state: EscalationState
) -> None:
    """Four desks, four different rows. `arrives too late` steps the clock past
    the escalation's window before the desk looks, so the queue refuses the close
    — a third outcome, distinct from answering and from silence, and the one most
    likely to be mishandled."""
    desk = factory(store(), esc.resolve)
    await conversation(desk, step_s=step_s)

    assert {h.outcome for h in desk.handled} == {outcome}, [str(h) for h in desk.handled]
    raised = await desk.store.get(desk.handled[0].escalation_id)  # type: ignore[attr-defined]
    assert raised is not None
    assert raised.state is state


@pytest.mark.discharges("ext:escalation_desk", "op:escalate")
async def test_a_handled_escalation_returns_the_customer_to_the_agent() -> None:
    """The round trip. The agent stops serving, the desk closes it, and the
    customer is served again — without anybody telling the agent directly."""
    desk = Desk.answers(store(), esc.resolve)
    await conversation(
        desk,
        said=["I want to speak to a human", f"where is my order {ORDER}"],
        turns=2,
    )

    assert {h.outcome for h in desk.handled} == {"resolved"}
    closed = await desk.store.get(desk.handled[0].escalation_id)  # type: ignore[attr-defined]
    assert closed is not None
    assert closed.state is EscalationState.RESOLVED
    assert closed.outcome is not None and closed.outcome.value == "resolved"


@pytest.mark.discharges("P-ESC-OUTCOME")
async def test_the_over_escalation_label_reaches_the_row() -> None:
    """The number that makes over-escalation measurable rather than arguable.

    A desk that only ever agrees with us tests nothing: the false-positive rate
    is supplied by the person who picked the ticket up, and this is the only
    place it can enter the system.
    """
    desk = Desk.says_agent_could_have(store(), esc.resolve, note="ordinary order status")
    await conversation(desk, turns=2)

    closed = await desk.store.get(desk.handled[0].escalation_id)  # type: ignore[attr-defined]
    assert closed is not None
    assert closed.outcome is not None and closed.outcome.value == "agent_could_have"
    assert closed.outcome_by == "desk-3"
    assert closed.outcome_note == "ordinary order status"
    assert closed.rule_id == "asked-for-human", "sliceable by the rule that raised it"


@pytest.mark.discharges("P-ESC-OWNS")
async def test_a_desk_cannot_close_its_own_customers_escalation() -> None:
    """The confused deputy of the human path, on the escalation record.

    A customer who could mark their own case resolved would make the
    over-escalation rate a number written by the party it measures.
    """
    desk = Desk.answers(store(), esc.resolve, name="C-1042")
    await conversation(desk, turns=2)

    assert {h.outcome for h in desk.handled} == {"refused"}
    assert all("customer" in h.detail for h in desk.handled)
    still_open = await desk.store.pending()  # type: ignore[attr-defined]
    assert len(still_open) == 1, "a refused close must not quietly resolve it"


@pytest.mark.discharges("P-ESC-TTL", "P-ESC-LAPSE")
async def test_nobody_comes_and_the_queue_says_so() -> None:
    """Nothing happens, which is correct — and the point is that nothing happens
    *quietly*. The escalation is still sitting there after the customer has given
    up, and no part of the system says so."""
    desk = Desk.never_comes(store(), esc.resolve)
    await conversation(desk)

    assert {h.outcome for h in desk.handled} == {"waiting"}
    assert await desk.store.pending(), "still queued, and nobody is alerted"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# What AgentTwin can now see that we cannot yet do. These pass by asserting the
# gap, and are the tests that should start failing when Tier 2 lands.
# --------------------------------------------------------------------------- #


@pytest.mark.documents_gap(
    "dissatisfaction is not an intent, so a customer repeating it carries none to "
    "repeat and no rule sees them (GAPS section 7)"
)
async def test_a_frustrated_customer_is_never_escalated_today() -> None:
    """The gap `repeated-intent` does **not** close — narrower now, and named.

    A customer who says "that is not good enough" four times has, by any
    operational standard, earned a person. Nothing notices: dissatisfaction is
    not an intent, so the router classifies none, and a fact about *the same
    intent repeated* cannot count turns that carry no intent at all.

    This test was written expecting `repeated-intent` to make it fail. It ships
    (F-025) and this still passes, which is the honest answer: a customer asking
    the same thing three times now reaches a person
    (`test_a_customer_asking_the_same_thing_three_times_reaches_a_person`), and a
    customer who is merely unhappy does not. What would cover it is the AOAS's
    deferred trigger *three failed resolution attempts*, and "failed" there needs
    a definition nobody has written — recorded as a gap rather than guessed at.
    """
    desk = Desk.answers(store(), esc.resolve)
    world = Live.start(load(WORLD))
    actor = StateMachineActor(
        opening="where is my order AB-10001",
        rules=[Rule(when=re.compile(r".*", re.S), say="that is not good enough")],
        max_turns=4,
        persistence="that is not good enough",
    )

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=desk.store,
        )
        await run_scenario(
            Scenario(name="a customer nobody helps", max_turns=4),
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=Clock(step_s=MINUTE),
        )

    assert await desk.store.pending() == ()  # type: ignore[attr-defined]
    assert desk.handled == [], "no escalation was ever raised, so the desk saw nothing"


@pytest.mark.discharges("op:escalate", "ext:escalation_desk")
async def test_the_clock_drives_expiry_rather_than_a_rewritten_row() -> None:
    """`contracts.Clock`, finally implemented by something.

    The lapse test in `test_escalation.py` has to reach into the store and
    rewrite `expires_at`, because the agent read the wall clock and no caller
    could move it. That tests the predicate, not the path: it proves
    `lapsed()` compares two numbers, and says nothing about a real
    conversation crossing a real window.

    Here the scenario's clock is the agent's clock. Nobody touches the record —
    time simply passes, and the customer is told the truth about it.
    """
    ticking = Clock(step_s=20 * MINUTE)
    desk = Desk.never_comes(store(), esc.resolve)
    world = Live.start(load(WORLD))
    actor = ScriptedActor(["I want to speak to a human", "any update?"])

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=patient(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=desk.store,
            clock=ticking,
        )
        record = await run_scenario(
            Scenario(name="nobody ever came", max_turns=2),
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            desk=desk,
            clock=ticking,
        )

    raised = desk.handled[0].escalation_id
    lapsed = await desk.store.get(raised)  # type: ignore[attr-defined]
    assert lapsed is not None
    assert lapsed.state is EscalationState.EXPIRED, "the window passed and nobody came"
    assert raised in record.reply, "the customer is told which reference lapsed"
    assert await desk.store.pending() == ()  # type: ignore[attr-defined]
