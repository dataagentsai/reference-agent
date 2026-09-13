"""Routing and the control loop.

The loop tests use a real in-process MCP server and a scripted model, so a
trajectory is exercised end to end with no network and at zero cost — which is
what makes running the whole suite on every prompt edit affordable.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import router
from support_agent import telemetry as tel
from support_agent.config import Budgets
from support_agent.contracts import (
    Completed,
    Failed,
    Identity,
    Intent,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    ToolCall,
    Usage,
)
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient, UnavailableClient
from support_agent.tools import META_SIDE_EFFECT, connect

SYSTEM = "You are a support agent."


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"lookups": 0}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        srv.state["lookups"] += 1  # type: ignore[attr-defined]
        return OrderOut(order_id=order_id, status="shipped")

    return srv


@pytest.fixture(autouse=True)
def exporter():
    """Configured once per test. `open_tools` must not reconfigure — doing so
    replaced the provider after the test had captured its exporter, and the
    spans went somewhere nobody was looking."""
    return tel.configure()


def open_tools(server):
    return connect(server, ledger=InMemoryLedger())


def says(text: str) -> ModelResponse:
    return ModelResponse(text=text, usage=Usage(input_tokens=10, output_tokens=5))


def calls(name: str, **arguments: object) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(id=f"tc_{name}", name=name, arguments=arguments),),
        usage=Usage(input_tokens=10, output_tokens=5),
    )


# --------------------------------------------------------------------------- #
# Routing. Refuse and Escalate never reach the model.
# --------------------------------------------------------------------------- #

ROUTE_CASES = [
    ("out of scope", "can I get a discount on this?", "refuse"),
    ("another customer", "what about my friend's order?", "refuse"),
    ("asks for a human", "just put me through to a human", "escalate"),
    ("lost in transit", "the courier says it is lost in transit", "escalate"),
    ("unambiguous with id", "where is my order AB-12345", "direct"),
    ("unambiguous without id", "where is my order?", "agentic"),
    ("write intent never direct", "cancel order AB-12345", "agentic"),
    ("multi-intent", "cancel AB-12345 and return CD-99999", "agentic"),
    ("unrecognised", "hello there", "agentic"),
]


@pytest.mark.parametrize(("name", "text", "kind"), ROUTE_CASES, ids=[c[0] for c in ROUTE_CASES])
@pytest.mark.discharges(
    "R-DISCOUNT", "R-OTHER-CUSTOMER", "esc:asked-for-human", "esc:lost-in-transit"
)
def test_routing(name: str, text: str, kind: str) -> None:
    assert router.route(text).kind == kind


@pytest.mark.discharges("R-DISCOUNT")
def test_refusal_is_checked_before_intent() -> None:
    """A request out of scope does not become in scope by mentioning an order."""
    assert router.route("discount on my order AB-12345?").kind == "refuse"


@pytest.mark.discharges("esc:asked-for-human")
def test_escalation_is_checked_before_intent() -> None:
    """Someone asking for a human should not be routed into a loop that tries
    to help first."""
    assert router.route("cancel AB-12345, actually get me a manager").kind == "escalate"


@pytest.mark.discharges("P-DIRECT", "AHC-0100")
def test_a_direct_route_carries_everything_the_handler_needs() -> None:
    decision = router.route("where is order AB-12345")
    assert decision.kind == "direct"
    assert decision.intent is Intent.ORDER_STATUS
    assert decision.args == {"order_id": "AB-12345"}


@pytest.mark.discharges("P-DIRECT-READS", "AHC-0100")
def test_writes_are_never_direct() -> None:
    """A deterministic path is cheaper; it is not a place to hide an
    irreversible effect."""
    for text in ("cancel order AB-12345", "return order AB-12345"):
        assert router.route(text).kind == "agentic"


@pytest.mark.discharges("AAC-0100", "AAC-0101", "AHC-0027", "R-DISCOUNT")
def test_the_route_and_its_reason_are_on_the_trace(exporter) -> None:
    """AAC-0100 — the serving route is recorded, with its reason."""
    router.route("can I get a discount")
    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert attrs[tel.ROUTE_KIND] == "refuse"
    assert "discount" in attrs[tel.ROUTE_REASON]
    assert attrs["agent.router.rules_version"] == "v1"


# --------------------------------------------------------------------------- #
# The loop.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0017")
async def test_a_plain_answer_terminates_immediately(server) -> None:
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "where is my order",
            identity=customer(),
            llm=ScriptedClient([says("It shipped yesterday.")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert isinstance(result, Completed)
    assert trace.steps == 1
    assert trace.termination is TerminationReason.GOAL_REACHED


@pytest.mark.discharges("AHC-0017", "op:get_order")
async def test_a_tool_call_then_an_answer(server) -> None:
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "where is AB-12345",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id="AB-1"), says("It has shipped.")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert isinstance(result, Completed)
    assert result.reply == "It has shipped."
    assert server.state["lookups"] == 1
    assert trace.steps == 2


@pytest.mark.discharges("AAC-0055", "AHC-0041")
async def test_the_step_budget_terminates_and_says_why(server) -> None:
    """AAC-0055 — hard termination under every condition. An unexplained stop is
    indistinguishable from a hang."""
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "loop forever",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id=f"AB-{i}") for i in range(20)]),
            tools=tools,
            system_prompt=SYSTEM,
            budgets=Budgets(max_steps=4),
        )
    assert trace.steps == 4
    assert trace.termination is TerminationReason.STEP_BUDGET_EXHAUSTED
    assert isinstance(result, Completed)


@pytest.mark.discharges("AAC-0054", "AAC-0109", "AHC-0042")
async def test_oscillation_is_caught_inside_the_budget(server) -> None:
    """**G2, closed.** This was written to justify filling a catalog gap, and
    the catalog filled it: AAC-0109 arrived at 0.12.0 saying exactly this —
    *"a loop that repeats three times and then stops within its step budget is
    inside every limit and has still made no progress."*

    AAC-0055 catches hard non-termination, AAC-0054 scores path efficiency, and
    neither catches A-B-A-B that stops in time. Second time in two days a finding
    from this runtime became an obligation, and the second time our own stale
    manifest hid it — see `test_g1_was_accepted_by_the_catalog`."""
    async with open_tools(server) as tools:
        _, trace = await agent_loop.run(
            "go in circles",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id="AB-1") for _ in range(10)]),
            tools=tools,
            system_prompt=SYSTEM,
            budgets=Budgets(max_steps=10),
            oscillation_threshold=3,
        )
    assert trace.termination is TerminationReason.OSCILLATION_DETECTED
    assert trace.steps < 10


@pytest.mark.discharges("AAC-0109", "AHC-0042")
async def test_argument_order_does_not_hide_an_oscillation(server) -> None:
    """A detector that thinks {"a":1,"b":2} differs from {"b":2,"a":1} never fires."""
    from support_agent.loop.plan import signature as _signature

    assert _signature("t", {"a": 1, "b": 2}) == _signature("t", {"b": 2, "a": 1})


@pytest.mark.discharges("AAC-0009", "AHC-0005", "AHC-0017")
async def test_provider_failure_is_a_declared_path_not_a_stack_trace(server) -> None:
    """AAC-0009. The customer sees a sentence; the operator sees the detail."""
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "anything",
            identity=customer(),
            llm=UnavailableClient(),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert isinstance(result, Failed)
    assert "trouble" in result.customer_message
    assert "unavailable" in result.detail
    assert trace.termination is TerminationReason.UNRECOVERABLE_ERROR


@pytest.mark.discharges("AHC-0037")
async def test_an_unknown_tool_is_reported_back_not_raised(server) -> None:
    """AAC-0051 — the model gets to choose again."""
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "do something impossible",
            identity=customer(),
            llm=ScriptedClient([calls("no_such_tool", x=1), says("Sorry, I cannot do that.")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert isinstance(result, Completed)
    assert trace.steps == 2


@pytest.mark.discharges("AAC-0052", "AHC-0037")
async def test_invalid_arguments_are_reported_back_not_raised(server) -> None:
    """AAC-0052. Nothing ran, so nothing was swallowed."""
    async with open_tools(server) as tools:
        result, _ = await agent_loop.run(
            "bad args",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id=123), says("Let me try again.")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert isinstance(result, Completed)
    assert server.state["lookups"] == 0


@pytest.mark.discharges("AHC-0007")
async def test_usage_accumulates_across_steps(server) -> None:
    async with open_tools(server) as tools:
        _, trace = await agent_loop.run(
            "two steps",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id="AB-1"), says("done")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    assert trace.usage.input_tokens == 20
    assert trace.usage.output_tokens == 10


@pytest.mark.discharges("AAC-0060", "AAC-0011", "B8")
async def test_the_trajectory_is_reconstructable_from_the_trace(server, exporter) -> None:
    """AAC-0060, as an M5 assertion — structure and ordering, no transcript."""
    async with open_tools(server) as tools:
        await agent_loop.run(
            "where is AB-12345",
            identity=customer(),
            llm=ScriptedClient([calls("get_order", order_id="AB-1"), says("shipped")]),
            tools=tools,
            system_prompt=SYSTEM,
        )
    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("agent.step") == 2
    assert "agent.tool" in names
    assert names[-1] == "agent.run"

    run_attrs = tel.attributes_of(exporter.get_finished_spans()[-1])
    assert run_attrs[tel.TERMINATION] == "goal_reached"
