"""How a turn ends when it runs out of something other than steps or money.

Two stops generation runs found missing: the model's output budget running out
mid-answer (AHC-0025, run 2), and the turn's own wall clock (AHC-0096, run 1).
Each is its own termination, never a clipped answer passed on as whole and never
a step stop, because the two call for different fixes. A third joined them when
AHC-0096 was widened (AHC 8703070): the caller leaving.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest
from agenttwin import Live, load, project
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    ToolCall,
    Usage,
)
from support_agent.llm import ScriptedClient
from support_agent.loop.ends import CALLER_LEFT
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import META_SIDE_EFFECT, connect

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


# --------------------------------------------------------------------------- #
# The caller has gone (AHC-0096). A departed caller ends the turn down the same
# path as the deadline: asked before each step and before a step's tools run,
# so no further model or tool call starts for a reply nobody will read.
# --------------------------------------------------------------------------- #


def gone_after(checks: int | None):
    """A `gone` that answers no for `checks` questions and yes after; `None` never."""
    asked = count()

    async def gone() -> bool:
        return checks is not None and next(asked) >= checks

    return gone


def looks(*orders: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=tuple(
            ToolCall(id=f"tc{i}", name="get_order", arguments={"order_id": o})
            for i, o in enumerate(orders)
        ),
        usage=USAGE,
    )


# (name, questions answered "still here", model calls, look-ups run, how it ends)
DEPARTURES = [
    ("a caller who stays", None, 2, 1, TerminationReason.GOAL_REACHED),
    ("gone before the first step", 0, 0, 0, TerminationReason.CALLER_GONE),
    ("gone while the model answered", 1, 1, 0, TerminationReason.CALLER_GONE),
    ("gone while the tools ran", 2, 1, 1, TerminationReason.CALLER_GONE),
]


@pytest.mark.discharges("AHC-0096")
@pytest.mark.parametrize(
    ("name", "checks", "model_calls", "lookups", "ends"), DEPARTURES, ids=[d[0] for d in DEPARTURES]
)
async def test_a_caller_who_has_gone_starts_no_further_call(
    name: str, checks: int | None, model_calls: int, lookups: int, ends: TerminationReason
) -> None:
    from support_agent import loop as agent_loop

    shop, ran = _shop()
    llm = ScriptedClient([looks("CN-1"), ModelResponse(text="It has shipped.", usage=USAGE)])
    async with connect(shop, requests=InMemoryRequests()) as tools:
        result, trace = await agent_loop.run(
            "where is CN-1",
            identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
            llm=llm,
            tools=tools,
            system_prompt="s",
            gone=gone_after(checks),
            now=still,
        )
    assert trace.termination is ends
    assert (len(llm.calls), len(ran)) == (model_calls, lookups)
    if ends is TerminationReason.CALLER_GONE:
        assert result.reply == CALLER_LEFT


def _shop() -> tuple[MCPServer, list[str]]:
    ran: list[str] = []
    shop = MCPServer("ecom")

    @shop.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> Shipped:
        """Look up an order."""
        ran.append(order_id)
        return Shipped(order_id=order_id, status="shipped")

    return shop, ran


class Shipped(BaseModel):
    order_id: str
    status: str


# (name, what the connection does after the body, model calls, the reply)
CONNECTIONS = [
    ("the browser waits", "held", 1, "Your order has shipped."),
    ("the browser closed the tab", "closed", 0, CALLER_LEFT),
]


@pytest.mark.discharges("AHC-0096")
@pytest.mark.parametrize(
    ("name", "connection", "model_calls", "reply"), CONNECTIONS, ids=[c[0] for c in CONNECTIONS]
)
async def test_chat_asks_the_connection_whether_the_caller_is_still_there(
    name: str, connection: str, model_calls: int, reply: str
) -> None:
    """Through the real `/chat` door, at the ASGI level, because a test client
    never closes a connection mid-request. Starlette's `is_disconnected` reads
    the next message without waiting: `http.disconnect` is a closed tab."""
    import asyncio
    import json
    import time

    from evals import issuer as issuing

    from support_agent import serve

    world = Live.start(load(WORLD))
    llm = ScriptedClient([ModelResponse(text="Your order has shipped.", usage=USAGE)])
    body = json.dumps({"text": "can you look into my orders please"}).encode()
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    never = asyncio.Event()

    async def receive() -> dict:
        if messages:
            return messages.pop(0)
        if connection == "closed":
            return {"type": "http.disconnect"}
        await never.wait()
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    tok = issuing.mint("C-1042", now=int(time.time()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat",
        "raw_path": b"/chat",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {tok}".encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=llm, tools=tools, store=InMemoryCheckpointStore(), clock=still)
        await serve.build(agent, issuer=issuing.issuer())(scope, receive, send)

    answered = json.loads(next(m["body"] for m in sent if m["type"] == "http.response.body"))
    assert len(llm.calls) == model_calls
    assert answered["reply"] == reply
