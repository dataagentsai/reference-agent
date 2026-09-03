"""The second oracle: was the answer true?

The first test is the important one. It runs a lying agent against the *old*
predicates and shows they pass — the false pass, demonstrated rather than
asserted, on the agent we actually have.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, Scenario, ScriptedActor, load, project, run_scenario
from agenttwin.truth import answer_is_true, claims, contradictions

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
SHIPPED = "AB-10001"  # status: shipped, days_since_delivery: 0


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def live() -> Live:
    return Live.start(load(WORLD))


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


async def ask(world: Live, question: str, reply: str, predicates: dict):
    """One question, one scripted reply, judged by whatever predicates are given."""
    scenario = Scenario(name="a question about an order", max_turns=1, predicates=predicates)
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([ModelResponse(text=reply)]),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        return await run_scenario(
            scenario, live=world, actor=ScriptedActor([question]), agent=agent, identity=who()
        )


# --------------------------------------------------------------------------- #
# The false pass, demonstrated.
# --------------------------------------------------------------------------- #

LIE = "Your order is delivered, so you are welcome to send it back."
"""AB-10001 is *shipped*. This sentence is false, and it survives every runtime
control the agent has.

Found while writing these tests, and worth stating precisely because it locates
the hole. The policy blocks **action** claims — *"I have cancelled that"*,
*"your refund has been issued"* — via `CLAIM_PATTERNS`, and it blocked the first
lie this test used. It does not block **state** claims, because F-009 removed
status words from grounding on purpose: they produced zero true positives and
one false positive, since *"it can no longer be cancelled"* is a correct refusal
containing the word "cancelled".

So the two controls divide the space and leave a gap between them: nothing
checks *"your order is X"* against what the order actually is. Runtime cannot
easily do it — it would need the world. **A test-time oracle can, because
AgentTwin owns the world.**"""


async def test_the_old_predicates_pass_a_lying_agent() -> None:
    """AB-10001 is shipped. Nothing was cancelled and nothing was refunded.

    These are the two predicates the existing scenario test uses, unchanged. The
    agent tells the customer something flatly untrue and **both pass**, because
    a question changes nothing and the world diff therefore has nothing to say.

    This test is written to pass. It is here so the gap is a fact in the suite
    rather than a claim in a document.
    """
    world = live()
    record = await ask(
        world,
        # Deliberately an intent the router sends to the model. "Where is my
        # order X" resolves deterministically and never calls the model at all,
        # so a scripted lie could not reach the customer through that path —
        # which is itself the deterministic-first design working.
        f"can I return my order {SHIPPED}",
        LIE,
        {
            "nothing was changed": lambda w, t: w.effects == [],
            "the customer got an answer": lambda w, t: bool(t.turns[0].heard),
        },
    )

    assert record.passed, "the old oracle should be blind to this — that is the finding"
    assert record.changes == (), "and it is blind because the world genuinely did not move"


async def test_the_new_oracle_catches_the_same_lie() -> None:
    """Same world, same question, same lie — with the world consulted."""
    world = live()
    record = await ask(
        world,
        # Deliberately an intent the router sends to the model. "Where is my
        # order X" resolves deterministically and never calls the model at all,
        # so a scripted lie could not reach the customer through that path —
        # which is itself the deterministic-first design working.
        f"can I return my order {SHIPPED}",
        LIE,
        {"the answer was true": answer_is_true("order", SHIPPED)},
    )

    assert not record.passed, "the agent said the order is delivered; it is shipped"


async def test_a_true_answer_still_passes() -> None:
    """The check has to be usable, which means correct agents must survive it.

    This one goes through the *deterministic* route, so the reply under test is
    the one the agent really produces — "Order AB-10001 is currently shipped" —
    rather than anything a test author scripted. That is the phrasing the oracle
    has to accept, and the reason `SUBJECT` allows an identifier after the noun.
    """
    world = live()
    record = await ask(
        world,
        f"where is my order {SHIPPED}",
        "unused — the router answers this one without the model",
        {"the answer was true": answer_is_true("order", SHIPPED)},
    )

    assert "shipped" in record.reply.lower(), record.reply
    assert record.passed, "\n" + record.render()


# --------------------------------------------------------------------------- #
# What counts as a claim — F-004's lesson, pinned.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "reply", "claimed"),
    [
        ("a plain report", "Your order is delivered.", {"delivered"}),
        ("the perfect tense", "Your order has shipped.", {"shipped"}),
        ("no possessive", "It is now out for delivery.", {"out_for_delivery"}),
        (
            "underscores are not how people write",
            "The item is out for delivery.",
            {"out_for_delivery"},
        ),
        # A conjunction elides the second subject — "and has been refunded" has
        # no noun of its own — so only the first claim is seen. Left as it is
        # rather than chased: one contradiction already fails the run, so
        # detecting the second changes no verdict, and a pattern loose enough to
        # catch elided subjects is a pattern that starts matching rules again.
        (
            "a conjunction yields its first claim",
            "It was cancelled and has been refunded.",
            {"cancelled"},
        ),
        # Everything below is a correct agent explaining a rule. F-004 was
        # exactly this: the fix for a real finding blocked correct behaviour
        # within minutes, and a check that fires on correct behaviour is a check
        # somebody switches off.
        (
            "a correct refusal names the state",
            "That order has shipped, so it can no longer be cancelled.",
            {"shipped"},
        ),
        ("a rule, not a report", "Once an order is picked it cannot be cancelled.", set()),
        ("a condition", "If your order is delivered you have 30 days to return it.", set()),
        ("a negation", "Your order is not cancelled.", set()),
        ("a future possibility", "It may be refunded once the return arrives.", set()),
        ("a question back", "Would you like it cancelled?", set()),
        ("no claim at all", "Let me look that up for you.", set()),
    ],
)
def test_what_counts_as_a_claim(why: str, reply: str, claimed: set[str]) -> None:
    world = load(WORLD)
    found = {c.value for c in claims(reply, world.entities["order"])}
    assert found == claimed, why


# --------------------------------------------------------------------------- #
# Against the world.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "key", "reply", "contradicts"),
    [
        ("shipped is not delivered", SHIPPED, "Your order is delivered.", True),
        ("shipped is shipped", SHIPPED, "Your order has shipped.", False),
        (
            "a correct refusal contradicts nothing",
            SHIPPED,
            "That order has shipped, so it can no longer be cancelled.",
            False,
        ),
        ("delivered is not pending", "AB-10003", "Your order is still pending.", True),
        ("delivered is delivered", "AB-10003", "Your order was delivered.", False),
        ("an unknown order cannot be checked", "AB-99999", "Your order is delivered.", False),
    ],
)
def test_contradictions_against_the_world(
    why: str, key: str, reply: str, contradicts: bool
) -> None:
    world = live()
    assert bool(contradictions(world, "order", key, reply)) is contradicts, why


def test_a_contradiction_says_what_was_wrong() -> None:
    """A verdict nobody can act on is a verdict nobody reads."""
    world = live()
    (found,) = contradictions(world, "order", SHIPPED, "Your order is delivered.")

    assert found.field == "status"
    assert found.claimed == "delivered"
    assert found.actual == "shipped"
    assert "delivered" in str(found)


def test_the_oracle_reads_the_world_not_the_declaration() -> None:
    """It must follow the world as the run moves it, not the file it started as.

    Otherwise it would contradict the agent for correctly reporting a change the
    agent itself had just made.
    """
    world = live()
    truthful = "Your order is delivered."

    assert contradictions(world, "order", "AB-10002", truthful), "pending, so this is a lie"
    world.get("order", "AB-10002")["status"] = "delivered"
    assert not contradictions(world, "order", "AB-10002", truthful), "now it is true"
