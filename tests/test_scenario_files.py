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
from agenttwin import Live, load, load_scenario, run_file
from evals.simulation import subject_for

from support_agent.contracts import ModelResponse, ToolCall, Usage
from support_agent.llm import ScriptedClient

SCENARIOS = sorted((Path(__file__).parent.parent / "scenarios").glob("*.yaml"))
ORDER = "AB-10003"


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

    async with subject_for(live, llm=asks_for_a_refund()) as subject:
        record, outcomes = await run_file(path, subject=subject, live=live)

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
