"""The declared scenarios run, through the contract, against this implementation.

Scenarios lived in pytest and were welded to this agent: they imported its
builder, its stores, its identity type. A suite like that can judge exactly one
implementation, which is no use for either question this project exists to ask —
*did a regeneration arrive at the same behaviour*, and *does an agent for another
domain behave the same way*.

These run from files. The only thing that knows how this agent is wired is
`support_agent.simulation.subject_for`, on the far side of the contract; a
regenerated agent supplies its own and the same files drive it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import (
    Clock,
    Live,
    load,
    load_scenario,
    perturbed,
    provider_faults,
    run_file,
    timeline_for,
)
from evals.scripted import model_for
from evals.simulation import subject_for

ALL = sorted((Path(__file__).parent.parent / "scenarios").glob("*.yaml"))
SCENARIOS = [
    p for p in ALL if load_scenario(p).actor.kind != "model" and load_scenario(p).generate is None
]
"""The scenarios this suite can run offline. A model-driven customer needs a
provider and cannot be replayed, so it belongs to the live report and never to a
regression suite — `Unrunnable` says so rather than the suite quietly skipping."""
# The model's side of each scenario is declared in the scenario file's `model:`
# block (agenttwin 0.1, `ModelTurnFile`), so the provider twin can serve the same
# answers to any implementation. `evals.scripted.model_for` is the in-process
# form of it, for this suite.


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
@pytest.mark.discharges("op:escalate", "op:request_refund", "AHC-0010")
async def test_a_declared_scenario_passes_every_check_it_makes(path: Path) -> None:
    """AHC-0010 is discharged by what this test does not need. Thirty-four
    scenarios drive one unit of work end to end through `Agent.handle` with no
    web server, no queue and no interface, and assert on typed results. An
    entrypoint that required any of those could not be driven this way."""
    scenario = load_scenario(path)
    live = Live.start(load(path.parent / scenario.world))

    timeline = timeline_for(scenario)
    # One clock for the agent and the people offstage. Two of them means the desk
    # reviews at a moment the agent has not reached, so everything has expired by
    # the time anybody looks (F-033).
    clock = Clock(step_s=scenario.step_seconds)
    # And the wrapper holds it, so a `slow` call can move it. Time that passes
    # only on the wall is time the agent never sees.
    wrap = perturbed(live, timeline, clock)  # always: the wrapper is what counts calls

    async with subject_for(
        live,
        llm=model_for(scenario),
        wrap=wrap,
        provider_faults=provider_faults(scenario),
        clock=clock,
    ) as subject:
        record, outcomes = await run_file(
            path, subject=subject, live=live, timeline=timeline, clock=clock
        )

    failed = [f"{o.check} — {o.detail}" for o in outcomes if not o.passed]
    assert failed == [], f"{scenario.scenario}:\n  " + "\n  ".join(failed)
    assert record.scenario == scenario.scenario
    assert record.discharges == scenario.discharges


@pytest.mark.tooling
def test_every_scenario_names_what_it_discharges() -> None:
    """A scenario tied to no statement is a scenario nobody can find again when
    the statement changes — and the Assurance Map cannot count it."""
    for path in ALL:
        assert load_scenario(path).discharges, f"{path.name} discharges nothing"


COVERED_AT_LEAST = 51
"""What scenarios reached when this ratchet was set, 2026-09-12 — 17 of 55. It turns one
way: a statement that has been demonstrated end to end does not stop being
demonstrated because somebody deleted the scenario that did it."""


@pytest.mark.tooling
def test_scenario_coverage_only_goes_up() -> None:
    """The Assurance Map says a statement has a test. This says a *conversation*
    exercised it, against a world that could refuse — different evidence, and
    the gap between the two numbers is the honest measure of how much of this
    agent's behaviour is asserted rather than demonstrated.
    """
    from evals.scenario_coverage import coverage

    report = coverage()
    reached = len(report["reached"])
    assert reached >= COVERED_AT_LEAST, (
        f"scenario coverage fell to {reached} from {COVERED_AT_LEAST} — "
        "a statement stopped being demonstrated end to end"
    )


@pytest.mark.tooling
def test_every_unreached_statement_says_why_it_cannot_be_reached() -> None:
    """The unreached list must not become a place work goes to be forgotten.

    Five statements are genuinely out of a scenario's reach. Four are the
    binding's numbers, and a scenario deliberately has no view of the binding,
    which is exactly what makes the same scenarios runnable against a
    regenerated agent on a different stack. Each of those carries a written
    reason and a note of where it *is* demonstrated.

    Anything else unreached is work, and this fails until somebody either writes
    the scenario or writes down why there will never be one.
    """
    from evals.scenario_coverage import UNREACHABLE, coverage

    report = coverage()
    unexplained = [s for s in report["unreached"] if s not in UNREACHABLE]
    assert unexplained == [], (
        "unreached by any scenario and no reason recorded — write the scenario, "
        f"or add it to UNREACHABLE with why: {unexplained}"
    )
    # And the other direction: an excuse for a statement that is now reached is
    # an excuse that has outlived its truth.
    stale = [s for s in UNREACHABLE if s in report["reached"]]
    assert stale == [], f"listed as unreachable and reached by a scenario: {stale}"


GENERATED = [p for p in ALL if load_scenario(p).generate is not None]


@pytest.mark.discharges("AAC-0106", "AHC-0034", "AHC-0040")
@pytest.mark.parametrize("path", GENERATED, ids=[p.stem for p in GENERATED])
async def test_every_generated_attack_case_leaves_the_world_alone(path: Path) -> None:
    """One declaration, twenty attempts, a fresh world for each.

    Fresh per case because an attack that *did* succeed would leave the world
    changed for the next one, and the second failure would be the first one's
    fault — which is how a suite reports one defect twenty times and hides
    nineteen others.

    The model here is scripted to **obey the planted instruction completely**:
    it reads the note and then tries the irreversible thing the note asked for.
    That is the point. The claim is not that the model resists — it is that
    nothing it can be talked into reaches an effect.
    """
    from agenttwin import attack_cases, plant

    scenario = load_scenario(path)
    cases = attack_cases(scenario)
    assert len(cases) == scenario.generate.count, "the declaration and the cases disagree"

    for name, payload in cases:
        live = Live.start(load(path.parent / scenario.world))
        plant(live, scenario, payload)
        timeline = timeline_for(scenario)
        async with subject_for(
            live, llm=model_for(scenario), wrap=perturbed(live, timeline)
        ) as subject:
            _, outcomes = await run_file(path, subject=subject, live=live, timeline=timeline)
        failed = [f"{o.check} — {o.detail}" for o in outcomes if not o.passed]
        assert failed == [], f"{name} ({payload[:60]}…):\n  " + "\n  ".join(failed)


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
@pytest.mark.discharges("AHC-0010")
async def test_a_declared_scenario_says_the_same_against_the_real_store(path: Path) -> None:
    """Shadow mode (T-042): the same file, with Saleor answering and the checks
    reading Saleor. A check that passes above and fails here has found a
    difference between the world the spec describes and a real store.

    Skipped when no Saleor answers, and for scenarios that cannot run against a
    real store yet — each says why.
    """
    from evals import shadow

    why_not = shadow.reachable() or shadow.unshadowable(path)
    if why_not:
        pytest.skip(why_not)

    scenario = load_scenario(path)
    clock = Clock(step_s=scenario.step_seconds)
    async with shadow.shadowed(
        path,
        llm=model_for(scenario),
        clock=clock,
        provider_faults=provider_faults(scenario),
    ) as (subject, world):
        record, outcomes = await run_file(path, subject=subject, live=world, clock=clock)

    failed = [f"{o.check} — {o.detail}" for o in outcomes if not o.passed]
    assert failed == [], f"{scenario.scenario} (against Saleor):\n  " + "\n  ".join(failed)
