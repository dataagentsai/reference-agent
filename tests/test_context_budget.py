"""What survives a checkpoint.

`assemble` has always bounded the transcript on the way to the model. That hid
the problem rather than solving it: the trim was invisible to the database, and
`Conversation.messages` had no cap at all — so a conversation that ran all day
wrote a larger row on every turn and read it back on the next, while the class
it lives on claims the trace/checkpoint split is what stops exactly that.

These tests are about the *stored* history, not the sent one.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import context as ctx
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, Message, ModelResponse, SideEffectClass, ToolCall
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import META_SIDE_EFFECT, connect


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

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def said(text: str) -> Message:
    return Message(role="user", content=text, provenance="user")


def replied(text: str) -> Message:
    return Message(role="assistant", content=text, provenance="operator")


# --------------------------------------------------------------------------- #
# The bound itself.
# --------------------------------------------------------------------------- #

SIZES = [
    ("well under", 10, 10_000, 10),
    ("exactly at the edge", 10, 1_000, 10),
    ("over, so the middle goes", 40, 1_000, None),
    ("far over, two units survive", 200, 100, 2),
]


@pytest.mark.parametrize(("name", "turns", "cap", "expected"), SIZES, ids=[c[0] for c in SIZES])
def test_how_much_survives(name: str, turns: int, cap: int, expected) -> None:
    history = tuple(said("x" * 100) for _ in range(turns))
    kept = ctx.bounded(history, max_chars=cap)

    if expected is not None:
        assert len(kept) == expected, name
    assert sum(len(m.content) for m in kept) <= cap or len(kept) == 2


def test_the_ends_are_what_survive() -> None:
    """The earliest turns establish the task; the latest are what is being
    answered. The middle is what nobody is using."""
    history = tuple(said(f"turn {i} " + "x" * 200) for i in range(20))
    kept = ctx.bounded(history, max_chars=600)

    assert kept[0].content.startswith("turn 0")
    assert kept[-1].content.startswith("turn 19")


def test_a_tool_call_is_never_separated_from_its_answer() -> None:
    """The failure `assemble` already paid for once, at a layer that would have
    persisted the broken transcript rather than merely sending it."""
    history: list[Message] = []
    for i in range(30):
        history.append(said("please look that up " + "x" * 100))
        history.append(
            Message(
                role="assistant",
                content="checking " + "x" * 100,
                tool_calls=(ToolCall(id=f"tc{i}", name="get_order", arguments={}),),
                provenance="operator",
            )
        )
        history.append(
            Message(
                role="tool",
                content="result " + "x" * 100,
                tool_call_id=f"tc{i}",
                tool_name="get_order",
                provenance="tool",
            )
        )

    kept = ctx.bounded(tuple(history), max_chars=900)
    called, answered = ctx.orphaned(kept)
    assert (called, answered) == (set(), set()), (
        "a call without its answer is a 400 from every provider"
    )


def test_an_already_broken_history_is_refused_rather_than_stored() -> None:
    """The guard runs even when nothing was trimmed. A transcript that arrives
    orphaned should not be written down as though it were fine."""
    broken = (
        Message(
            role="assistant",
            content="checking",
            tool_calls=(ToolCall(id="tc1", name="get_order", arguments={}),),
            provenance="operator",
        ),
    )
    with pytest.raises(ctx.BrokenTranscript):
        ctx.bounded(broken, max_chars=10_000)


# --------------------------------------------------------------------------- #
# Through the agent. What the store holds, and what the caller is handed.
# --------------------------------------------------------------------------- #


async def test_a_long_conversation_stops_growing(server) -> None:
    store = InMemoryCheckpointStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([ModelResponse(text="y" * 400)] * 40),
            tools=tools,
            store=store,
            history_chars=2_000,
        )
        conversation = None
        sizes = []
        for i in range(12):
            _, conversation = await agent.handle(
                f"tell me about thing {i} " + "x" * 200,
                identity=customer(),
                conversation=conversation,
            )
            sizes.append(len(await store.latest(conversation.conversation_id)))

    assert sizes[-1] <= sizes[len(sizes) // 2] * 1.5, f"still growing: {sizes}"
    assert max(sizes) < 12_000, f"a bound that does not bind: {max(sizes)}"


async def test_the_caller_holds_what_the_store_holds(server) -> None:
    """The returned conversation is the bounded one. A caller holding a larger
    history than the store does would be looking at state that exists nowhere."""
    store = InMemoryCheckpointStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([ModelResponse(text="y" * 300)] * 20),
            tools=tools,
            store=store,
            history_chars=1_500,
        )
        conversation = None
        for i in range(10):
            _, conversation = await agent.handle(
                f"question {i} " + "x" * 150, identity=customer(), conversation=conversation
            )

    stored = Conversation.decode(await store.latest(conversation.conversation_id))
    assert stored.messages == conversation.messages
    assert stored.turn_count == conversation.turn_count == 10, "the count outlives the transcript"


async def test_bounding_does_not_touch_what_rules_read(server) -> None:
    """`recent`, `turn_count` and `escalated_rules` are facts, not transcript.

    They are already bounded on their own terms and must not be collateral
    damage of a history trim — a Tier 2 rule that stopped firing because the
    prose got long would be a very hard defect to find.
    """
    store = InMemoryCheckpointStore()
    async with connect(server, ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([ModelResponse(text="y" * 400)] * 20),
            tools=tools,
            store=store,
            history_chars=800,
        )
        conversation = None
        for i in range(8):
            _, conversation = await agent.handle(
                f"thing {i} " + "x" * 300, identity=customer(), conversation=conversation
            )

    assert conversation.turn_count == 8
    assert len(conversation.recent) == 8
    assert len(conversation.messages) < 16, "the transcript was trimmed"
