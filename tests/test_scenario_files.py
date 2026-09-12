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
    Live,
    load,
    load_scenario,
    perturbed,
    provider_faults,
    run_file,
    timeline_for,
)
from evals.simulation import subject_for

from support_agent.contracts import ModelResponse, ToolCall, Usage
from support_agent.llm import ScriptedClient

SCENARIOS = sorted((Path(__file__).parent.parent / "scenarios").glob("*.yaml"))
ORDER = "AB-10003"


def model_for(stem: str) -> ScriptedClient:
    """The scripted model a scenario needs, by name.

    **The default is an empty script**, which raises if the model is called at
    all — so a scenario that expects a deterministic answer proves it from
    outside rather than asserting it from within. A scenario needing the loop
    says so by appearing here.

    Scripting the agent's reasoning at all is the honest limit of this slice: a
    scenario declares what the *customer* says and what must be true, and the
    model's choices are still the suite's. Live runs replace this, scored as
    pass rates rather than pass/fail.
    """
    script = SCRIPTS.get(stem)
    return script() if script is not None else ScriptedClient([])


def tries_the_return_late() -> ScriptedClient:
    """Hesitates, then asks — by which time the world has moved on. The model
    tries the return on the last turn and the order system refuses it."""
    wait = ModelResponse(
        text="Of course, take your time.", usage=Usage(input_tokens=5, output_tokens=2)
    )
    attempt = ModelResponse(
        tool_calls=(ToolCall(id="r1", name="open_return_request", arguments={"id": "AB-10003"}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    explain = ModelResponse(
        text="I am sorry — that order is past its return window now.",
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    return ScriptedClient([wait, wait, attempt, explain, explain])


def answers_plainly() -> ScriptedClient:
    """One ordinary answer. The first attempt at it is throttled, so the client
    that survives to produce this is the one under test."""
    return ScriptedClient(
        [
            ModelResponse(
                text="Yes — you can return it within thirty days of delivery.",
                usage=Usage(input_tokens=5, output_tokens=2),
            )
        ]
        * 3
    )


def asks_for_a_human() -> ScriptedClient:
    """Asking for a person is a Tier 1 route and never reaches the model."""
    return ScriptedClient([])


def wants_to_cancel() -> ScriptedClient:
    """The customer's first turn is answered by the deterministic route, which is
    what makes the read happen; the model is first called on the second turn."""
    act = ModelResponse(
        tool_calls=(ToolCall(id="c2", name="cancel_order", arguments={"id": "AB-10002"}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    claim = ModelResponse(
        text="That order was still pending, so I have cancelled it.",
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    return ScriptedClient([act, claim, claim, claim])


def asks_for_a_refund() -> ScriptedClient:
    """A model that requests the refund, then waits like the customer does."""
    plan = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="request_refund", arguments={"order_id": ORDER}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    patience = ModelResponse(
        text="Let me check on that for you.", usage=Usage(input_tokens=5, output_tokens=2)
    )
    return ScriptedClient([plan, *[patience] * 6])


SCRIPTS = {
    "refund-needs-a-person": asks_for_a_refund,
    "nobody-comes": asks_for_a_refund,
    "stale-read-then-refused": wants_to_cancel,
    "nobody-picks-up-the-escalation": asks_for_a_human,
    "the-provider-throttles": answers_plainly,
    "the-window-closes-while-they-talk": tries_the_return_late,
}
"""Scenarios that need the loop, and the reasoning the suite supplies for them.
Anything absent gets an empty script, so reaching the model at all raises."""


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
@pytest.mark.discharges("op:escalate", "op:request_refund")
async def test_a_declared_scenario_passes_every_check_it_makes(path: Path) -> None:
    scenario = load_scenario(path)
    live = Live.start(load(path.parent / scenario.world))

    timeline = timeline_for(scenario)
    wrap = perturbed(live, timeline)  # always: the wrapper is what counts calls

    async with subject_for(
        live,
        llm=model_for(path.stem),
        wrap=wrap,
        provider_faults=provider_faults(scenario),
    ) as subject:
        record, outcomes = await run_file(path, subject=subject, live=live, timeline=timeline)

    failed = [f"{o.check} — {o.detail}" for o in outcomes if not o.passed]
    assert failed == [], f"{scenario.scenario}:\n  " + "\n  ".join(failed)
    assert record.scenario == scenario.scenario
    assert record.discharges == scenario.discharges


@pytest.mark.tooling
def test_every_scenario_names_what_it_discharges() -> None:
    """A scenario tied to no statement is a scenario nobody can find again when
    the statement changes — and the Assurance Map cannot count it."""
    for path in SCENARIOS:
        assert load_scenario(path).discharges, f"{path.name} discharges nothing"


COVERED_AT_LEAST = 19
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
