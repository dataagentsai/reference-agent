"""A scenario's `model:` block, as this agent's scripted client.

The script used to live in `tests/test_scenario_files.py` as Python built from
this agent's own response types, so the scenarios could drive exactly one
implementation. It now lives in each scenario file, where the provider twin can
serve it to any implementation over the wire (`python -m agenttwin run`), and
this is the in-process path the regression suite keeps: the same answers, as
`ScriptedClient`, with no server in between.
"""

from __future__ import annotations

from pathlib import Path

from agenttwin import ScenarioFile, load_scenario

from support_agent.contracts import ModelResponse, ToolCall, Usage
from support_agent.llm import ScriptedClient

SCENARIOS = Path(__file__).parent.parent / "scenarios"


def model_for(scenario: ScenarioFile | Path | str) -> ScriptedClient:
    """The scripted model a scenario declares.

    **An absent `model:` block is an empty script**, which raises if the model is
    called at all — so a scenario that expects a deterministic answer proves it
    from outside rather than asserting it from within.

    Accepts the scenario, its path, or its file stem (what `scripts/` pass).
    """
    if isinstance(scenario, str):
        scenario = SCENARIOS / f"{scenario}.yaml"
    if isinstance(scenario, Path):
        scenario = load_scenario(scenario)
    return ScriptedClient(
        ModelResponse(
            text=turn.says,
            tool_calls=tuple(
                ToolCall(id=f"s{n}_{i}", name=name, arguments=dict(arguments))
                for i, call in enumerate(turn.calls, start=1)
                for name, arguments in call.items()
            ),
            usage=Usage(input_tokens=5, output_tokens=2),
        )
        for n, turn in enumerate(scenario.scripted_answers(), start=1)
    )


__all__ = ["model_for"]
