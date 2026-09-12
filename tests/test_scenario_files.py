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
from agenttwin import Live, load, load_scenario, perturbed, run_file, timeline_for
from evals.simulation import subject_for

from support_agent.contracts import ModelResponse, ToolCall, Usage
from support_agent.llm import ScriptedClient

SCENARIOS = sorted((Path(__file__).parent.parent / "scenarios").glob("*.yaml"))
ORDER = "AB-10003"


def model_for(scenario) -> ScriptedClient:
    """The scripted model this scenario needs.

    Scripting the model here is the honest limit of this slice: a scenario
    declares what the *customer* says and what must be true, and the agent's own
    reasoning is still fixed by the suite. Live runs against a real model are
    what replace this, and they are scored as pass rates rather than pass/fail.
    """
    if "cancel" in scenario.objective.lower():
        return wants_to_cancel()
    return asks_for_a_refund()


def wants_to_cancel() -> ScriptedClient:
    look = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10002"}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    act = ModelResponse(
        tool_calls=(ToolCall(id="c2", name="cancel_order", arguments={"id": "AB-10002"}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    claim = ModelResponse(
        text="That order was still pending, so I have cancelled it.",
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    return ScriptedClient([look, act, claim, claim, claim])


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


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
@pytest.mark.discharges("op:escalate", "op:request_refund")
async def test_a_declared_scenario_passes_every_check_it_makes(path: Path) -> None:
    scenario = load_scenario(path)
    live = Live.start(load(path.parent / scenario.world))

    timeline = timeline_for(scenario)
    wrap = perturbed(live, timeline) if scenario.perturbations else None

    async with subject_for(live, llm=model_for(scenario), wrap=wrap) as subject:
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
