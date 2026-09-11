"""Actors — and what a persistent customer finds that a script does not.

The last test is the one written expecting a failure: idempotency keys are
derived from run + step + iteration, and every conversational turn is a new run.
A customer who asks three times is three runs, three key spaces, and — if
nothing else stops it — three effects.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agenttwin import (
    Determinism,
    Live,
    Rule,
    Scenario,
    ScriptedActor,
    StateMachineActor,
    Transcript,
    load,
    project,
    run_scenario,
)
from agenttwin.actor import ModelActor, weakest

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
DELIVERED = "AB-10003"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def who(extra: frozenset[str] = frozenset()) -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES | extra)


def live() -> Live:
    return Live.start(load(WORLD))


def calls(name: str, **args) -> ModelResponse:
    return ModelResponse(tool_calls=(ToolCall(id="tc", name=name, arguments=args),))


# --------------------------------------------------------------------------- #
# Determinism is declared, never inferred.
# --------------------------------------------------------------------------- #

WEAKEST_CASES = [
    ("one scripted", (Determinism.SCRIPTED,), Determinism.SCRIPTED),
    (
        "scripted + state machine",
        (Determinism.SCRIPTED, Determinism.STATE_MACHINE),
        Determinism.STATE_MACHINE,
    ),
    (
        "any model-driven",
        (Determinism.SCRIPTED, Determinism.MODEL_DRIVEN),
        Determinism.MODEL_DRIVEN,
    ),
    ("none at all", (), Determinism.SCRIPTED),
]


@pytest.mark.parametrize(
    ("name", "classes", "expected"), WEAKEST_CASES, ids=[c[0] for c in WEAKEST_CASES]
)
@pytest.mark.tooling
def test_a_run_is_as_reproducible_as_its_weakest_actor(
    name: str, classes: tuple, expected: Determinism
) -> None:
    """A reader must never have to guess whether a result can be reproduced."""
    assert weakest(*classes) is expected


@pytest.mark.tooling
def test_a_model_driven_actor_refuses_to_be_built_casually() -> None:
    """It trades replay for realism. The seam is declared so the cost is
    visible; building it by accident is how a regression suite stops being one."""
    with pytest.raises(NotImplementedError, match="trades replay for realism"):
        ModelActor()


# --------------------------------------------------------------------------- #
# A state machine reaches states a script cannot.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
async def test_the_actor_branches_on_what_the_agent_actually_said() -> None:
    """The whole reason for the class. The path taken depends on the agent's
    behaviour rather than on a plan made before the run."""
    actor = StateMachineActor(
        opening="can I return AB-10003?",
        rules=[
            Rule(
                when=re.compile(r"\bcannot|can't|unable\b", re.I),
                say="why not?",
                label="pushed back",
            ),
            Rule(
                when=re.compile(r"\breturn opened|have opened\b", re.I),
                say="thank you",
                label="satisfied",
            ),
        ],
        max_turns=3,
    )

    assert actor.next("") == "can I return AB-10003?"
    assert actor.next("I have opened a return for you.") == "thank you"
    assert actor.path == ["opening", "satisfied"]

    other = StateMachineActor(
        opening="can I return AB-10003?",
        rules=actor.rules,
        max_turns=3,
    )
    assert other.next("") == "can I return AB-10003?"
    assert other.next("I cannot do that.") == "why not?"
    assert other.path == ["opening", "pushed back"]


@pytest.mark.tooling
def test_a_scripted_actor_cannot_tell_a_good_agent_from_a_deaf_one() -> None:
    """Stated as a test because it is the limitation that motivates the class
    above: the same transcript comes back either way."""
    good = ScriptedActor(["a", "b"])
    deaf = ScriptedActor(["a", "b"])
    assert good.next("") == deaf.next("")
    assert good.next("A helpful, correct reply.") == deaf.next("Banana.")


@pytest.mark.tooling
def test_an_actor_stops_rather_than_running_forever() -> None:
    """An actor that never stops is a scenario that never ends, and a suite that
    hangs is a suite nobody runs."""
    actor = StateMachineActor(opening="hello", rules=[], max_turns=2, persistence="still there?")
    assert actor.next("") == "hello"
    assert actor.next("Hi.") == "still there?"
    assert actor.next("Hi again.") is None


# --------------------------------------------------------------------------- #
# A scenario, end to end.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0003")
async def test_a_scenario_asserts_on_the_world_and_records_its_class() -> None:
    world = live()
    scenario = Scenario(
        name="a customer asks about an order",
        max_turns=2,
        discharges=("AAC-0003",),
        predicates={
            "nothing was changed": lambda w, t: w.effects == [],
            "the customer got an answer": lambda w, t: bool(t.turns and t.turns[0].heard),
        },
    )
    actor = ScriptedActor([f"where is my order {DELIVERED}"])

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([ModelResponse(text="It was delivered.")]),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        record = await run_scenario(scenario, live=world, actor=actor, agent=agent, identity=who())

    assert record.passed, "\n" + record.render()
    assert record.determinism_class == "scripted"
    assert record.changes == ()


# --------------------------------------------------------------------------- #
# The one written expecting a failure.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0056", "P-CANCEL", "op:cancel_order", "AAC-0110")
async def test_a_persistent_customer_cannot_get_the_same_effect_three_times() -> None:
    """Idempotency keys are `run + step + iteration`, and **every conversational
    turn is a new run**. A customer who asks three times is three runs and three
    key spaces, so the ledger cannot see across them.

    Whether that matters depends entirely on whether something *else* stops the
    repeat. For `cancel_order` the world does: a cancelled order is no longer
    cancellable. This asks the question about a customer who simply keeps
    asking — the case a single-turn test cannot express.
    """
    world = live()
    actor = StateMachineActor(
        opening="please cancel AB-10002",
        rules=[],
        max_turns=3,
        persistence="are you sure? please cancel AB-10002",
    )

    scenario = Scenario(
        name="a persistent customer asks three times",
        max_turns=3,
        discharges=("AAC-0047", "AAC-0056"),
        predicates={
            "the effect happened at most once": lambda w, t: w.count("cancel_order") <= 1,
            "the customer was not told it happened twice": lambda w, t: (
                sum(1 for turn in t.turns if "have cancelled" in turn.heard.lower()) <= 1
            ),
        },
    )

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        # The model complies every single time it is asked — which is the point.
        # The controls must hold without the model's cooperation.
        agent = ep.build(
            llm=ScriptedClient(
                [
                    calls("cancel_order", id="AB-10002"),
                    ModelResponse(text="I have cancelled that order."),
                ]
                * 3
            ),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        record = await run_scenario(scenario, live=world, actor=actor, agent=agent, identity=who())

    assert record.passed, "\n" + record.render() + "\n" + Transcript(record and []).render()
    assert record.determinism_class == "state_machine"
