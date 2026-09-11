"""Invariants, and the three places one declaration has to hold.

F-011 found that 12 of 29 generated cases described a world that cannot exist.
The fix is a declaration, so what these tests actually assert is that *the same
declaration* reaches every consumer — the generator, the loader and a
perturbation. A constraint honoured in one place and not the others is worse
than no constraint, because the two disagree silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agenttwin import load
from agenttwin.loader import InvalidWorld, resolve_spec
from agenttwin.perturbation import IncoherentPerturbation, StaleRead
from agenttwin.projection import Live
from agenttwin.world import Condition, Entity, Field_, Invariant

sys.path.insert(0, str(Path(__file__).parent.parent / "evals"))
from generate_golden import generate  # noqa: E402

WORLDS = Path(__file__).parent.parent / "worlds"

DELIVERY = Invariant(
    name="delivery age implies delivery happened",
    when=Condition(field="days_since_delivery", at_least=1),
    then=Condition(field="status", equals=("delivered", "returned", "refunded")),
    because="an order that has not been delivered cannot be N days since delivery",
)
FINAL_SALE = Invariant(
    name="final sale orders are never returned",
    when=Condition(field="final_sale", equals=(True,)),
    then=Condition(field="status", not_equals=("returned",)),
)


@pytest.mark.parametrize(
    ("why", "row", "violated"),
    [
        ("the case F-011 found", {"status": "pending", "days_since_delivery": 30}, True),
        ("shipped is not delivered", {"status": "shipped", "days_since_delivery": 31}, True),
        ("cancelled is not delivered", {"status": "cancelled", "days_since_delivery": 1}, True),
        ("delivered is the whole point", {"status": "delivered", "days_since_delivery": 30}, False),
        ("returned was delivered first", {"status": "returned", "days_since_delivery": 5}, False),
        ("refunded was delivered first", {"status": "refunded", "days_since_delivery": 5}, False),
        ("day zero says nothing", {"status": "pending", "days_since_delivery": 0}, False),
        ("the boundary is one, not zero", {"status": "shipped", "days_since_delivery": 1}, True),
        # Silence about absent fields is required, not lenient: the generator
        # evaluates partial rows while it is still choosing values.
        ("silent when the field is absent", {"status": "pending"}, False),
    ],
)
@pytest.mark.tooling
def test_delivery_invariant(why: str, row: dict, violated: bool) -> None:
    assert DELIVERY.violated_by(row) is violated, why


@pytest.mark.parametrize(
    ("why", "row", "violated"),
    [
        ("final sale cannot be returned", {"final_sale": True, "status": "returned"}, True),
        ("final sale can be delivered", {"final_sale": True, "status": "delivered"}, False),
        ("an ordinary order can be returned", {"final_sale": False, "status": "returned"}, False),
        ("silent when the field is absent", {"status": "returned"}, False),
    ],
)
@pytest.mark.tooling
def test_final_sale_invariant(why: str, row: dict, violated: bool) -> None:
    assert FINAL_SALE.violated_by(row) is violated, why


@pytest.mark.parametrize("world_file", ["clothing.yaml", "electronics.yaml"])
@pytest.mark.tooling
def test_no_generated_case_describes_an_impossible_world(world_file: str) -> None:
    """F-011, pinned. This is the assertion the finding is about."""
    world = load(WORLDS / world_file)
    cases = generate(world)
    spec = world.entities["order"]

    impossible = [c for c in cases if spec.violations(c["row"])]
    assert impossible == [], f"{len(impossible)} of {len(cases)} cases cannot exist"


@pytest.mark.parametrize("world_file", ["clothing.yaml", "electronics.yaml"])
@pytest.mark.tooling
def test_the_constraints_actually_prune_something(world_file: str) -> None:
    """A constraint that prunes nothing is a constraint nobody needed.

    Asserted rather than assumed because the failure is invisible: if
    `filter_func` were silently ignored, every other test here would still pass.
    """
    world = load(WORLDS / world_file)
    spec = world.entities["order"]
    loose = generate(world, constrained=False)

    pruned = [c for c in loose if spec.violations(c["row"])]
    assert len(pruned) >= 10, "the unconstrained generator should still produce fiction"


@pytest.mark.parametrize("world_file", ["clothing.yaml", "electronics.yaml"])
@pytest.mark.tooling
def test_constraining_does_not_cost_the_boundaries(world_file: str) -> None:
    """The one way this fix could do real damage.

    Pruning is only safe if it removes impossible rows and not *interesting*
    ones. Day 30 and day 31 differ by one and by everything, and a constraint
    that swallowed either would look like a smaller, cleaner golden set.
    """
    world = load(WORLDS / world_file)
    window = next(
        c.at_most
        for c in world.systems["ecom"].actions["open_return_request"].allowed_when
        if c.at_most is not None
    )
    days = {c["row"]["days_since_delivery"] for c in generate(world)}

    assert {window, window + 1} <= days, f"lost the boundary at {window}/{window + 1}"


@pytest.mark.tooling
def test_a_seeded_row_that_cannot_exist_fails_at_load(tmp_path: Path) -> None:
    """The loader's job: fail loudly rather than produce verdicts about a world
    nobody meant to write. Unlike a bad enum, nothing downstream notices this."""
    source = (WORLDS / "clothing.yaml").read_text()
    broken = source.replace(
        "{id: AB-10002, customer_id: C-1042, total: 2499, status: pending, days_since_delivery: 0",
        "{id: AB-10002, customer_id: C-1042, total: 2499, status: pending, days_since_delivery: 9",
    )
    assert broken != source, "the fixture row moved; update this test"

    path = tmp_path / "broken.yaml"
    path.write_text(broken)

    # The copy's relative spec path no longer reaches, so the spec is named.
    with pytest.raises(InvalidWorld, match="delivery age implies delivery happened"):
        load(path, spec=resolve_spec(WORLDS / "clothing.yaml"))


@pytest.mark.tooling
def test_an_invariant_about_an_unknown_field_fails_at_load(tmp_path: Path) -> None:
    """The invariant is the agent spec's now, so the typo goes into the spec."""
    spec = resolve_spec(WORLDS / "clothing.yaml")
    original = spec.read_text()
    source = original.replace(
        "when: {field: days_since_delivery, at_least: 1}",
        "when: {field: delivered_on, at_least: 1}",
        1,
    )
    assert source != original, "the invariant moved; update this test"
    typo = tmp_path / "typo.aoas.yaml"
    typo.write_text(source)

    with pytest.raises(InvalidWorld, match="delivered_on"):
        load(WORLDS / "clothing.yaml", spec=typo)


@pytest.mark.tooling
def test_a_perturbation_cannot_leave_the_world_impossible() -> None:
    """The third consumer, and the one that reads as a pass when it is wrong.

    A stale read that moves a delivered order to `cancelled` while it still
    carries a delivery age is a bug in the scenario. Without this it runs, the
    agent behaves oddly against an impossible world, and the run looks like a
    finding about the agent.
    """
    world = load(WORLDS / "clothing.yaml")
    live = Live.start(world)

    with pytest.raises(IncoherentPerturbation, match="delivery age"):
        StaleRead(
            tool="get_order", entity="order", key="AB-10003", sets={"status": "cancelled"}
        ).apply(live)

    assert live.get("order", "AB-10003")["status"] == "delivered", "the world moved anyway"


@pytest.mark.tooling
def test_a_coherent_perturbation_still_applies() -> None:
    """The check must not block the perturbations the suite depends on."""
    world = load(WORLDS / "clothing.yaml")
    live = Live.start(world)

    StaleRead(tool="get_order", entity="order", key="AB-10002", sets={"status": "picked"}).apply(
        live
    )

    assert live.get("order", "AB-10002")["status"] == "picked"


@pytest.mark.tooling
def test_an_entity_with_no_invariants_forbids_nothing() -> None:
    """The default has to stay silent — most entities declare none."""
    entity = Entity(fields={"status": Field_(type="text")})
    assert entity.violations({"status": "anything"}) == ()
