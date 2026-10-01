"""F-027 — every declared position is reached, and a block means what it says.

`Position` declares five places a rule may run. Two were called; a rule handed
to the other three was accepted, versioned, and silently never ran — and no test
failed, because no test asserted that a configured position is reached. This is
that test.

What a block *means* differs by position, which is the design rather than an
inconsistency: before the model and before the reply there is no lesser thing to
do than end the turn, while around a tool call there is — the model is told the
action did not happen and may choose again.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    ToolCall,
    Usage,
)
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import META_SIDE_EFFECT, connect

ORDER = "AB-1"


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"reads": 0}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        srv.state["reads"] += 1  # type: ignore[attr-defined]
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def refuse(ctx: pol.Context) -> pol.Verdict:
    """A rule that blocks wherever it is placed, so the position is the variable."""
    return pol.block("planted", "planted rule")


def wants_a_look() -> ScriptedClient:
    """Call the tool, then answer from what came back."""
    return ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="tc", name="get_order", arguments={"order_id": ORDER}),),
                usage=Usage(input_tokens=5, output_tokens=2),
            ),
            ModelResponse(text="It has shipped.", usage=Usage(input_tokens=5, output_tokens=2)),
        ]
    )


# (position, the result the customer gets, did the tool run, model calls used)
POSITIONS = [
    (pol.Position.PRE_MODEL, "Refused", 0, 0),
    (pol.Position.POST_MODEL, "Refused", 1, 2),
    (pol.Position.PRE_TOOL, "Completed", 0, 2),
    (pol.Position.POST_TOOL, "Completed", 1, 2),
    (pol.Position.REPLY, "Refused", 1, 2),
]


@pytest.mark.discharges("AHC-0093", "AHC-0094", "AAC-0091")
@pytest.mark.parametrize(
    ("position", "outcome", "ran", "calls"), POSITIONS, ids=[p[0].value for p in POSITIONS]
)
async def test_a_rule_at_every_position_is_reached(
    server, position: pol.Position, outcome: str, ran: int, calls: int
) -> None:
    llm = wants_a_look()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=InMemoryCheckpointStore(),
            policy_rules={position: (refuse,)},
        )
        result, _ = await agent.handle("tell me about something", identity=customer())

    assert type(result).__name__ == outcome, f"{position.value} did not take effect: {result}"
    if outcome == "Refused":
        assert result.rule_id == "planted", "a refusal names the rule that caused it"
        # F-066: inside the loop the reason was the word "refused", and the
        # rule's own explanation was dropped; the reply screen kept it.
        assert result.reason == "planted rule", "a refusal carries the rule's reason"
    assert server.state["reads"] == ran, "the tool ran when the rule said it should not"
    assert len(llm.calls) == calls, "the model was asked more or fewer times than expected"


@pytest.mark.discharges("AHC-0093")
async def test_a_blocked_call_is_answered_so_the_model_can_choose_again(server) -> None:
    """The PRE_TOOL semantics, stated: the action did not happen, the turn did
    not end, and what the model is told says which of those it is."""
    llm = wants_a_look()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=InMemoryCheckpointStore(),
            policy_rules={pol.Position.PRE_TOOL: (refuse,)},
        )
        await agent.handle("tell me about something", identity=customer())

    told = llm.calls[-1].messages[-1]
    assert told.role == "tool"
    assert "blocked before it ran" in told.content
    assert server.state["reads"] == 0


@pytest.mark.discharges("AHC-0093")
async def test_a_blocked_result_never_enters_context(server) -> None:
    """The POST_TOOL semantics: the effect happened and this cannot undo it, so
    all it does is refuse to carry what came back."""
    llm = wants_a_look()
    async with connect(server, requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=InMemoryCheckpointStore(),
            policy_rules={pol.Position.POST_TOOL: (refuse,)},
        )
        await agent.handle("tell me about something", identity=customer())

    told = llm.calls[-1].messages[-1]
    assert "shipped" not in told.content, "the result the rule refused reached the model"
    assert "withheld by planted" in told.content
    assert server.state["reads"] == 1, "the call still happened — only the result was withheld"
