"""The fourth oracle: what should have happened and did not.

The first test is the one that matters. It runs an agent that does nothing at
all against a world that owed a refund, checks it with **the other three
oracles**, and shows all three pass. Written to pass, so the gap doc 31 found is
a fact in the suite rather than a claim in a document.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, nothing_was_omitted, omitted, owed
from agenttwin.truth import contradictions

WORLDS = Path(__file__).parent.parent / "worlds"
RETURNED = "AB-10009"


def world_owing_a_refund() -> Live:
    """A world where a return has been received and the money is not back yet.

    Seeded here rather than in the world file: `worlds/clothing.yaml` describes
    an opening situation for a support conversation, and an order sitting in
    `returned` is a state the run should *resolve*, not one every scenario
    should start from.
    """
    live = Live.start(load(WORLDS / "clothing.yaml"))
    live.rows["order"][RETURNED] = {
        "id": RETURNED,
        "customer_id": "C-1042",
        "status": "returned",
        "days_since_delivery": 12,
        "final_sale": False,
    }
    return live


# --------------------------------------------------------------------------- #
# The blindness, demonstrated.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_the_other_three_oracles_pass_an_agent_that_did_nothing() -> None:
    """The refund was owed. The agent did not issue it, said something true, and
    changed nothing.

    - **world diff** — empty, and correctly so: nothing was supposed to change
      except the thing that did not happen.
    - **truth** — the reply is accurate. "Your return is being processed" is not
      a lie.
    - **bounds** — nothing was exceeded; it did almost nothing.

    Three passes for a run that failed. This is what "the failure mode with no
    evidence" means, and why a fourth oracle is a different instrument rather
    than a stricter setting on an existing one.
    """
    live = world_owing_a_refund()
    world_0 = live.snapshot()
    reply = "Your return has been received and is being processed."

    assert live.effects == [], "world diff: nothing changed"
    assert live.snapshot() == world_0, "world diff: world₀ and world₁ are identical"
    assert not contradictions(live, "order", RETURNED, reply), "truth: the reply is accurate"
    assert len(live.effects) <= 3, "bounds: nothing near any ceiling"

    # And the fourth one sees it.
    assert omitted(live, world_0), "omission: a refund was owed and never issued"


# --------------------------------------------------------------------------- #
# What is owed.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_an_obligation_is_derived_from_the_world_not_the_scenario() -> None:
    live = world_owing_a_refund()
    (obligation,) = owed(live.world, live.snapshot())

    assert obligation.action == "issue_refund"
    assert obligation.key == RETURNED
    assert "returned" in obligation.because, "name the state that created the debt"


@pytest.mark.tooling
def test_a_world_that_owes_nothing_reports_nothing() -> None:
    """The seeded orders are shipped, pending and delivered — none returned."""
    live = Live.start(load(WORLDS / "clothing.yaml"))
    assert owed(live.world, live.snapshot()) == ()


@pytest.mark.parametrize(
    ("why", "status", "is_owed"),
    [
        ("a received return owes the money", "returned", True),
        ("already paid back", "refunded", False),
        ("still with the customer", "delivered", False),
        ("never arrived", "shipped", False),
        ("called off", "cancelled", False),
    ],
)
@pytest.mark.tooling
def test_which_states_create_a_debt(why: str, status: str, is_owed: bool) -> None:
    live = Live.start(load(WORLDS / "clothing.yaml"))
    live.rows["order"]["AB-10099"] = {
        "id": "AB-10099",
        "customer_id": "C-1042",
        "status": status,
        "days_since_delivery": 12,
        "final_sale": False,
    }
    assert bool(owed(live.world, live.snapshot())) is is_owed, why


# --------------------------------------------------------------------------- #
# Discharging it.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_doing_the_owed_thing_clears_it() -> None:
    live = world_owing_a_refund()
    world_0 = live.snapshot()

    assert omitted(live, world_0), "owed before"
    live.effects.append(("issue_refund", RETURNED))
    assert not omitted(live, world_0), "discharged after"


@pytest.mark.tooling
def test_obligations_are_read_from_the_opening_state_not_the_final_one() -> None:
    """The subtlety the module exists to get right.

    `issue_refund` sets the status to `refunded`, so asking the *final* world
    what it owes returns nothing — for the agent that paid **and** for the agent
    that did nothing. Reading world₁ would reintroduce exactly the blindness
    being removed.
    """
    live = world_owing_a_refund()
    world_0 = live.snapshot()

    # The agent refunds: the row moves, the obligation is gone from world₁.
    live.rows["order"][RETURNED]["status"] = "refunded"
    assert owed(live.world, live.snapshot()) == (), "world₁ owes nothing either way"

    # Reading world₁ would call this a pass. Reading world₀ asks the real
    # question — was it owed, and was it done — and this agent did not do it.
    assert omitted(live, world_0), "status moved without the effect being recorded"

    live.effects.append(("issue_refund", RETURNED))
    assert not omitted(live, world_0)


@pytest.mark.tooling
def test_doing_it_for_a_different_order_does_not_count() -> None:
    """An obligation is per row. Refunding somebody else is not discharging this."""
    live = world_owing_a_refund()
    world_0 = live.snapshot()
    live.effects.append(("issue_refund", "AB-10003"))

    assert omitted(live, world_0)


@pytest.mark.tooling
def test_the_predicate_reads_as_prose_in_a_scenario() -> None:
    live = world_owing_a_refund()
    world_0 = live.snapshot()
    check = nothing_was_omitted(world_0)

    assert not check(live, None), "owed and not done"
    live.effects.append(("issue_refund", RETURNED))
    assert check(live, None)


@pytest.mark.tooling
def test_a_verdict_names_what_was_missed() -> None:
    """A failure saying only 'an obligation was omitted' sends someone to read
    the world file to find out which."""
    live = world_owing_a_refund()
    (missed,) = omitted(live, live.snapshot())

    assert "issue_refund" in str(missed)
    assert RETURNED in str(missed)
    assert "returned" in str(missed)


@pytest.mark.tooling
def test_a_second_world_can_owe_something_different() -> None:
    """The reason the obligation is declared rather than hand-written.

    `electronics.yaml` does not declare `required_when`, so it owes nothing —
    and a scenario author who had typed the obligation into a test would now be
    asserting a rule that world never made.
    """
    live = Live.start(load(WORLDS / "electronics.yaml"))
    live.rows["order"]["AB-10099"] = {
        "id": "AB-10099",
        "customer_id": "C-1042",
        "status": "returned",
        "days_since_delivery": 12,
        "final_sale": False,
    }
    assert owed(live.world, live.snapshot()) == ()
