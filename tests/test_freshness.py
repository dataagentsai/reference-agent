"""AHC-0107 / AAC-0113 — acting on a belief that has gone out of date.

The order's own declaration says `concurrent_writers: [warehouse, carrier]`,
which is the specification saying out loud that a read of `status` is a claim
about the past. The gap between that read and the action taken on it is exactly
as long as the model took to think.

The far system does refuse the stale write — that is `stale-read-then-refused`,
and it passes. What it cannot do is stop the agent having already composed a
reply from the stale value, and AAC-0113 asks for both halves for that reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.binding import FRESH_FOR_S
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    Usage,
)
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.loop import freshness
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
PENDING = "AB-10002"


def caller() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


# (name, when the row was read, when the action is planned, whether it is stale)
# The window is 30 seconds — the time a customer takes to answer one question.
AGES = [
    ("just read", 100, 100, False),
    ("read a moment ago", 100, 120, False),
    ("read exactly a window ago", 100, 130, False),
    ("read a second past the window", 100, 131, True),
    ("read long ago", 100, 4000, True),
]


@pytest.mark.discharges("AHC-0107")
@pytest.mark.parametrize(("name", "read", "act", "stale"), AGES, ids=[c[0] for c in AGES])
def test_when_a_belief_has_gone_out_of_date(name: str, read: int, act: int, stale: bool) -> None:
    fresh = freshness.Freshness(window_s=30)
    fresh.saw(PENDING, read)
    assert fresh.stale(PENDING, act) is stale


@pytest.mark.discharges("AHC-0107")
def test_a_row_this_run_never_read_is_not_stale() -> None:
    """It has no belief to be old.

    Re-reading here would invent a lookup the run never needed and make every
    first action cost two calls — and what governs an action taken blind is the
    far system's own preconditions, which have not gone anywhere.
    """
    fresh = freshness.Freshness(window_s=30)
    assert fresh.stale(PENDING, 10_000) is False


@pytest.mark.discharges("AHC-0107")
def test_a_binding_that_declared_no_window_turns_the_mechanism_off() -> None:
    """`None` is the honest default for a world with one writer: nothing there
    goes stale in the sense this is about, and a window invented here would buy
    a re-read before every irreversible action for no reason."""
    fresh = freshness.Freshness(window_s=None)
    fresh.saw(PENDING, 0)
    assert fresh.stale(PENDING, 10_000) is False


# (name, the tool surface, what `first_read` should choose)
SURFACES = [
    ("a read is offered", [("get_order", SideEffectClass.READ)], "get_order"),
    (
        "the first read in the spec's order",
        [("get_order", SideEffectClass.READ), ("peek", SideEffectClass.READ)],
        "get_order",
    ),
    (
        "reads come after writes",
        [("cancel_order", SideEffectClass.IRREVERSIBLE), ("get_order", SideEffectClass.READ)],
        "get_order",
    ),
    ("nothing can read", [("cancel_order", SideEffectClass.IRREVERSIBLE)], None),
]


@pytest.mark.discharges("AHC-0107")
@pytest.mark.parametrize(("name", "surface", "chosen"), SURFACES, ids=[c[0] for c in SURFACES])
def test_which_tool_is_used_to_look_again(
    name: str, surface: list[tuple[str, SideEffectClass]], chosen: str | None
) -> None:
    """The specification's order decides, not this module.

    A surface offering two ways to read the same row would make this a choice,
    and a choice made in the harness would be the harness inventing policy.
    """
    registry = ToolRegistry(
        tools=tuple(
            ToolSpec(
                name=name,
                description=name,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=effect,
            )
            for name, effect in surface
        )
    )
    found = registry.first_read()
    assert (found.name if found else None) == chosen


class Slow:
    """A model that takes forty seconds to think, which is the whole point.

    The window is thirty. Time passing *while the model reasons* is what makes a
    read stale by the time the action it informed is planned — not time passing
    during the tool calls, which are the fast part. Wrapping the client rather
    than the tools puts the delay where it actually is.
    """

    def __init__(self, clock: MovingClock, responses: list[ModelResponse]) -> None:
        self.clock, self.inner = clock, ScriptedClient(responses)

    async def complete(self, request):
        self.clock.at += 40
        return await self.inner.complete(request)


class MovingClock:
    """A clock the test moves, because the whole subject is time passing."""

    def __init__(self) -> None:
        self.at = 1_000

    def __call__(self) -> int:
        return self.at


@pytest.mark.discharges("AHC-0107", "AAC-0113", "op:cancel_order")
async def test_a_cancellation_planned_on_an_old_read_reads_again_first() -> None:
    """The whole thing, end to end.

    The model reads the order, the conversation takes longer than the window,
    and the model then asks to cancel. What must happen is a read before the
    cancel — and the fresh value going back to the model, so what it does next
    is planned against what is true rather than against what it remembered.
    """
    world = Live.start(load(WORLD))
    clock = MovingClock()
    look = ModelResponse(
        tool_calls=(ToolCall(id="r1", name="get_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    act = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    done = ModelResponse(
        text="That order is cancelled.", usage=Usage(input_tokens=5, output_tokens=2)
    )

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([look, act, done, done]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            clock=clock,
            fresh_for_s=FRESH_FOR_S,
        )
        # The read happens at 1000; the model's next request arrives well past
        # the window, which is the ordinary case and not an exotic one.
        clock.at = 1_000
        result, _ = await agent.handle(f"please cancel {PENDING}", identity=caller())

    reads = [name for name, _ in world.effects]
    assert "cancel_order" in reads, f"the cancellation never happened: {world.effects}"
    assert result.kind in {"completed", "refused"}, result


@pytest.mark.discharges("AHC-0107", "AAC-0113")
async def test_the_row_is_read_again_before_the_action_lands() -> None:
    """The ordering that matters: the re-read is *before*, not a check after.

    An irreversible action is exactly the case where explaining afterwards has
    no value. Asserted on the tool traffic the world saw, in order.
    """
    seen: list[str] = []
    world = Live.start(load(WORLD))
    clock = MovingClock()

    def watch(tool: str, handler):
        async def wrapped(**arguments):
            seen.append(tool)
            return await handler(**arguments)

        wrapped.__name__ = handler.__name__
        wrapped.__doc__ = handler.__doc__
        wrapped.__signature__ = handler.__signature__  # type: ignore[attr-defined]
        wrapped.__annotations__ = handler.__annotations__
        return wrapped

    look = ModelResponse(
        tool_calls=(ToolCall(id="r1", name="get_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    act = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    done = ModelResponse(text="Done.", usage=Usage(input_tokens=5, output_tokens=2))

    async with connect(project(world, wrap=watch), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=Slow(clock, [look, act, act, done, done]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            clock=clock,
            fresh_for_s=FRESH_FOR_S,
        )
        await agent.handle(f"please cancel {PENDING}", identity=caller())

    assert seen.count("get_order") >= 2, f"the row was never read again: {seen}"
    assert seen.index("cancel_order") > 1, (
        f"the cancellation ran before the second read — the ordering is the control: {seen}"
    )


@pytest.mark.discharges("AHC-0107", "AAC-0113", "op:cancel_order")
async def test_a_row_that_moved_underneath_the_run_stops_the_action() -> None:
    """The half the re-read exists for, and the half the other tests cannot see.

    Somebody else ships the order between the read that informed the plan and
    the action planned on it. The cancellation must not be attempted on the
    value that was believed, and the model must be given what is now true so it
    can plan against that instead.

    The change is driven from outside the agent — the world is moved directly,
    the way the warehouse would — because a case where the system caused the
    change is testing something else (AAC-0113).
    """
    world = Live.start(load(WORLD))
    clock = MovingClock()
    look = ModelResponse(
        tool_calls=(ToolCall(id="r1", name="get_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    act = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    gave_up = ModelResponse(
        text="That order has shipped, so I cannot cancel it.",
        usage=Usage(input_tokens=5, output_tokens=2),
    )

    class Warehouse:
        """The other writer. Ships the order while the model is thinking."""

        def __init__(self) -> None:
            self.inner, self.calls = ScriptedClient([look, act, gave_up, gave_up]), 0

        async def complete(self, request):
            self.calls += 1
            clock.at += 40
            if self.calls == 2:  # after the read, before the cancellation is planned
                world.rows["order"][PENDING]["status"] = "shipped"
            return await self.inner.complete(request)

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=Warehouse(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            clock=clock,
            fresh_for_s=FRESH_FOR_S,
        )
        result, _ = await agent.handle(f"please cancel {PENDING}", identity=caller())

    assert world.effects == [], f"a cancellation ran on a row that had moved: {world.effects}"
    assert world.rows["order"][PENDING]["status"] == "shipped"
    assert "cannot cancel" in result.reply, result
