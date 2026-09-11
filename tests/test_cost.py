"""Pricing, and the ceiling that can actually stop a run."""

from __future__ import annotations

from decimal import Decimal

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import telemetry as tel
from support_agent.config import Budgets
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    ToolCall,
    Usage,
)
from support_agent.cost import PRICES, Meter, Price, UnknownPrice, cost_of, price_of
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.tools import META_SIDE_EFFECT, connect

MODEL = "openai/gpt-oss-120b"
FLAT = {"m": Price(Decimal("1.00"), Decimal("2.00"))}


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@pytest.fixture
def server():
    srv = MCPServer("ecom")

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def calls(order_id: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(id="tc", name="get_order", arguments={"order_id": order_id}),),
        usage=Usage(input_tokens=500_000, output_tokens=500_000),
    )


# --------------------------------------------------------------------------- #
# Pricing. Unknown must never mean free.
# --------------------------------------------------------------------------- #


def test_an_unknown_model_raises_rather_than_pricing_at_zero() -> None:
    """A missing price that prices at zero is a ceiling that never fires — the
    failure is silent, unbounded, and first visible on an invoice."""
    with pytest.raises(UnknownPrice):
        price_of("some-model-nobody-priced")


def test_every_approved_model_has_a_price() -> None:
    """The allowlist and the price map must not drift apart."""
    from support_agent.config import Settings

    for model in Settings(provider_api_key="k").approved_models:
        assert price_of(model) is not None


COST_CASES = [
    ("one million in", Usage(input_tokens=1_000_000), Decimal("1.00")),
    ("one million out", Usage(output_tokens=1_000_000), Decimal("2.00")),
    ("half and half", Usage(input_tokens=500_000, output_tokens=500_000), Decimal("1.50")),
    ("nothing", Usage(), Decimal("0")),
]


@pytest.mark.parametrize(("name", "usage", "expected"), COST_CASES, ids=[c[0] for c in COST_CASES])
def test_cost_of_a_call(name: str, usage: Usage, expected: Decimal) -> None:
    assert cost_of(usage, FLAT["m"]) == expected


def test_money_is_decimal_not_float() -> None:
    """Money accumulating in binary floating point drifts, and a budget
    comparison wrong in the last place is a budget that fires late."""
    meter = Meter("m", ceiling_usd="1.00", prices=FLAT)
    for _ in range(10):
        meter.record(Usage(input_tokens=100_000))
    assert meter.spend == Decimal("1.00")
    assert isinstance(meter.spend, Decimal)


def test_cached_input_is_billed_at_its_own_rate_where_one_exists() -> None:
    price = Price(Decimal("1.00"), Decimal("2.00"), cached_input_per_mtok=Decimal("0.10"))
    assert cost_of(Usage(cached_input_tokens=1_000_000), price) == Decimal("0.10")


def test_cached_input_falls_back_to_the_input_rate() -> None:
    assert cost_of(Usage(cached_input_tokens=1_000_000), FLAT["m"]) == Decimal("1.00")


# --------------------------------------------------------------------------- #
# The meter.
# --------------------------------------------------------------------------- #


def test_the_meter_resolves_its_price_at_construction() -> None:
    """An unknown model fails when the run is set up, not three calls in."""
    with pytest.raises(UnknownPrice):
        Meter("nonexistent", ceiling_usd=1.0)


@pytest.mark.discharges("AHC-0030")
def test_exceeded_and_remaining() -> None:
    meter = Meter("m", ceiling_usd="1.00", prices=FLAT)
    meter.record(Usage(input_tokens=600_000))
    assert not meter.exceeded
    assert meter.remaining == Decimal("0.40")
    meter.record(Usage(input_tokens=600_000))
    assert meter.exceeded
    assert meter.remaining == Decimal("0")


PER_TASK_CASES = [
    ("two successes", 2, Decimal("0.50")),
    ("one success", 1, Decimal("1.00")),
    ("none succeeded", 0, None),
]


@pytest.mark.parametrize(
    ("name", "successes", "expected"), PER_TASK_CASES, ids=[c[0] for c in PER_TASK_CASES]
)
@pytest.mark.discharges("AAC-0008")
def test_cost_per_successful_task(name: str, successes: int, expected: Decimal | None) -> None:
    """AAC-0008. An agent that fails cheaply four times and succeeds on the fifth
    was not cheap, and nothing succeeding is None — not infinity, and certainly
    not the total."""
    meter = Meter("m", ceiling_usd="10.00", prices=FLAT)
    meter.record(Usage(input_tokens=1_000_000))
    assert meter.per_successful_task(successes) == expected


# --------------------------------------------------------------------------- #
# The ceiling at P4. Only the loop can see that fourteen calls are one task.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0093", "AHC-0030", "AHC-0041")
async def test_the_ceiling_stops_the_run(server) -> None:
    meter = Meter(MODEL, ceiling_usd="0.60")
    async with connect(server, ledger=InMemoryLedger()) as tools:
        _, trace = await agent_loop.run(
            "expensive",
            identity=customer(),
            llm=ScriptedClient([calls(f"AB-{i}") for i in range(10)]),
            tools=tools,
            system_prompt="s",
            budgets=Budgets(max_steps=10),
            meter=meter,
        )
    assert trace.termination is TerminationReason.COST_CEILING_REACHED
    assert trace.steps < 10


@pytest.mark.discharges("AHC-0030")
async def test_a_run_inside_its_budget_is_untouched(server) -> None:
    meter = Meter(MODEL, ceiling_usd="100.00")
    async with connect(server, ledger=InMemoryLedger()) as tools:
        _, trace = await agent_loop.run(
            "cheap",
            identity=customer(),
            llm=ScriptedClient([calls("AB-1"), ModelResponse(text="done")]),
            tools=tools,
            system_prompt="s",
            meter=meter,
        )
    assert trace.termination is TerminationReason.GOAL_REACHED
    assert trace.spend_usd > 0


@pytest.mark.discharges("AHC-0030")
async def test_a_single_call_may_overshoot_the_ceiling(server) -> None:
    """Stated rather than hidden. The ceiling is checked between calls because
    you cannot un-spend one; `max_output_tokens` is the per-call backstop."""
    meter = Meter(MODEL, ceiling_usd="0.01")
    async with connect(server, ledger=InMemoryLedger()) as tools:
        _, trace = await agent_loop.run(
            "one expensive call",
            identity=customer(),
            llm=ScriptedClient([calls("AB-1"), ModelResponse(text="done")]),
            tools=tools,
            system_prompt="s",
            meter=meter,
        )
    assert trace.spend_usd > 0.01
    assert trace.termination is TerminationReason.COST_CEILING_REACHED


@pytest.mark.discharges("AAC-0104", "AHC-0007")
async def test_spend_and_tenant_are_on_the_trace(server, exporter) -> None:
    """AAC-0104 — spend attributable to tenant, feature and route."""
    meter = Meter(MODEL, ceiling_usd="100.00")
    async with connect(server, ledger=InMemoryLedger()) as tools:
        await agent_loop.run(
            "anything",
            identity=customer(),
            llm=ScriptedClient([ModelResponse(text="done", usage=Usage(input_tokens=1000))]),
            tools=tools,
            system_prompt="s",
            meter=meter,
        )
    run = next(s for s in exporter.get_finished_spans() if s.name == "agent.run")
    attrs = tel.attributes_of(run)
    assert attrs[tel.TENANT] == "C-1042"
    assert attrs[tel.COST_USD] > 0


def test_the_default_price_map_is_dated_and_flagged() -> None:
    """These figures are an input to a budget, not a quotation. If the map grows
    silently this test is the reminder that it must be verified."""
    assert set(PRICES) == {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    }


# --------------------------------------------------------------------------- #
# F-019 — the ceiling, reached through the one place the agent is entered.
# --------------------------------------------------------------------------- #

CEILINGS = [
    # (name, max_cost_usd, the termination the turn must end on)
    ("a ceiling below one call's cost stops the task", 0.000001, "cost_ceiling_reached"),
    ("a generous ceiling lets the task finish", 10.0, "goal_reached"),
]


@pytest.mark.discharges("Q-COST", "AHC-0030", "AAC-0008")
@pytest.mark.parametrize(("name", "ceiling", "ends_on"), CEILINGS, ids=[c[0] for c in CEILINGS])
async def test_the_cost_ceiling_is_reachable_from_the_entrypoint(
    name: str, ceiling: float, ends_on: str
) -> None:
    from pathlib import Path

    from agenttwin import Live, load, project

    from support_agent import entrypoint as ep
    from support_agent import identity as ident
    from support_agent.config import Settings, resolve
    from support_agent.contracts import Identity, ModelResponse, ToolCall
    from support_agent.idempotency import InMemoryLedger
    from support_agent.llm import ScriptedClient
    from support_agent.state import InMemoryCheckpointStore
    from support_agent.tools import connect

    world = Live.start(load(Path(__file__).parent.parent / "worlds" / "clothing.yaml"))
    spend = Usage(input_tokens=1_000, output_tokens=1_000)  # ~$0.0009 at the configured price
    script = [
        ModelResponse(
            tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10003"}),),
            usage=spend,
        ),
        ModelResponse(
            tool_calls=(ToolCall(id="c2", name="get_order", arguments={"id": "AB-10002"}),),
            usage=spend,
        ),
        ModelResponse(text="Your order AB-10003 has been delivered.", usage=spend),
    ]
    config = resolve(
        Settings(provider_api_key="k", resolution="mock", sealed=True, max_cost_usd=ceiling)
    )
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient(script), tools=tools, store=InMemoryCheckpointStore(), config=config
        )
        result, _ = await agent.handle(
            # No single intent, so the router hands it to the loop — the only route
            # that spends. A status question would be answered without the model.
            "I need some help with a couple of my recent purchases",
            identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
        )

    assert getattr(result, "termination", None) == ends_on, result


def test_an_unpriced_model_fails_at_build_not_mid_conversation() -> None:
    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    with pytest.raises(UnknownPrice):
        ep.build(
            llm=None,  # never reached: the failure is at composition
            tools=None,
            store=InMemoryCheckpointStore(),
            metering=lambda: Meter("a-model-nobody-priced", ceiling_usd=1.0),
        )
