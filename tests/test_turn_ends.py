"""How a turn ends when it runs out of something other than steps or money.

Two stops generation runs found missing: the model's output budget running out
mid-answer (AHC-0025, run 2), and the turn's own wall clock (AHC-0096, run 1).
Each is its own termination, never a clipped answer passed on as whole and never
a step stop, because the two call for different fixes.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.contracts import Identity, ModelResponse, TerminationReason, Usage
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
USAGE = Usage(input_tokens=5, output_tokens=2)


def still() -> int:
    return 1_000


def racing():
    """Seventy seconds pass every time anybody looks — past the 60-second turn."""
    ticks = count(1_000, 70)
    return lambda: next(ticks)


# (name, what the model returns, the clock, how the turn must end)
ENDS = [
    (
        "an answer that fits",
        ModelResponse(text="Your order has shipped.", usage=USAGE, stop_reason="stop"),
        still,
        TerminationReason.GOAL_REACHED,
    ),
    (
        "an answer cut off by the output budget",
        ModelResponse(
            text="Your order has shipped and the refund of", usage=USAGE, stop_reason="length"
        ),
        still,
        TerminationReason.OUTPUT_LENGTH_REACHED,
    ),
    (
        "a turn past its wall clock",
        ModelResponse(text="Your order has shipped.", usage=USAGE, stop_reason="stop"),
        racing(),
        TerminationReason.DEADLINE_REACHED,
    ),
]


@pytest.mark.discharges("AHC-0025", "AHC-0096")
@pytest.mark.parametrize(("name", "said", "clock", "ends"), ENDS, ids=[e[0] for e in ENDS])
async def test_how_the_turn_ends(
    name: str, said: ModelResponse, clock, ends: TerminationReason
) -> None:
    world = Live.start(load(WORLD))
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=ScriptedClient([said] * 4),
            tools=tools,
            store=InMemoryCheckpointStore(),
            clock=clock,
        )
        who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
        result, _ = await agent.handle("can you look into my orders please", identity=who)

    termination = getattr(result, "termination", None) or TerminationReason.GOAL_REACHED
    assert termination is ends, result
    if ends is not TerminationReason.GOAL_REACHED:
        # Handed on (loop-exhausted reads the termination), never sent as whole.
        assert said.text not in result.reply, "a clipped or late answer was passed on"


@pytest.mark.discharges("AHC-0103", "AHC-0017")
async def test_a_transcript_assembly_cannot_send_is_a_failed_turn(monkeypatch) -> None:
    """F-065. Assembly refuses to send a call without its result, and raised
    `BrokenTranscript` outside any handler: the loop let it out, so the caller
    got a plain 500 and the customer no labelled reply. It ends the turn as
    Failed, with the reason for the operator and nothing internal for the
    customer."""
    from support_agent import context as ctx
    from support_agent.contracts import Failed

    def broken(**_: object) -> ctx.Assembly:
        raise ctx.BrokenTranscript("assembly orphaned tool calls ['tc'] and results []")

    monkeypatch.setattr("support_agent.loop.ctx.assembled", broken)
    world = Live.start(load(WORLD))
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]), tools=tools, store=InMemoryCheckpointStore(), clock=still
        )
        who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
        result, _ = await agent.handle("can you look into my orders please", identity=who)

    assert isinstance(result, Failed), result
    assert "orphaned" in result.detail and "orphaned" not in result.customer_message
