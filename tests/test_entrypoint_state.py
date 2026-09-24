"""The drivable surface, and what survives between turns.

Every test here drives the same entrypoint an evaluation will — AHC-0010. That
is the point: a test that reconstructs the system by hand is testing its own
wiring, not the agent.
"""

from __future__ import annotations

import pytest
from evals import durable
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.config import Settings, resolve
from support_agent.contracts import (
    Completed,
    ConversationId,
    Escalated,
    Failed,
    Identity,
    Message,
    ModelResponse,
    Refused,
    RunId,
    SideEffectClass,
    ToolCall,
    Usage,
)
from support_agent.llm import ScriptedClient, UnavailableClient
from support_agent.requests import InMemoryRequests
from support_agent.state import Conversation, FileCheckpointStore, InMemoryCheckpointStore
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
    srv.state = {"model_calls": 0}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def open_tools(server):
    return connect(server, requests=InMemoryRequests())


def says(text: str) -> ModelResponse:
    return ModelResponse(text=text, usage=Usage(input_tokens=5, output_tokens=2))


def calls(name: str, **arguments: object) -> ModelResponse:
    return ModelResponse(
        tool_calls=(ToolCall(id="tc", name=name, arguments=arguments),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )


def agent_with(tools, llm=None, store=None, config=None, escalations=None) -> ep.Agent:
    return ep.build(
        llm=llm or ScriptedClient([says("ok")]),
        tools=tools,
        store=store or InMemoryCheckpointStore(),
        config=config,
        escalations=escalations,
    )


# --------------------------------------------------------------------------- #
# Dispatch. Only one of four routes reaches the model.
# --------------------------------------------------------------------------- #

FREE_ROUTES = [
    ("refusal", "can I get a discount?", Refused),
    ("escalation", "put me through to a human", Escalated),
]


@pytest.mark.parametrize(("name", "text", "expected"), FREE_ROUTES, ids=[c[0] for c in FREE_ROUTES])
@pytest.mark.discharges("R-DISCOUNT", "esc:asked-for-human")
async def test_refusal_and_escalation_never_call_the_model(
    server, name: str, text: str, expected: type
) -> None:
    """A scripted client with nothing in it: reaching the model would raise."""
    llm = ScriptedClient([])
    async with open_tools(server) as tools:
        agent = agent_with(tools, llm=llm, escalations=durable.RememberedEscalations())
        result, _ = await agent.handle(text, identity=customer())
    assert isinstance(result, expected)
    assert llm.calls == []


@pytest.mark.discharges("P-DIRECT", "AHC-0100")
async def test_a_direct_route_answers_without_the_model(server) -> None:
    """The deterministic path. No model call, and the reply is templated."""
    llm = ScriptedClient([])
    async with open_tools(server) as tools:
        result, _ = await agent_with(tools, llm=llm).handle(
            "where is my order AB-12345", identity=customer()
        )
    assert isinstance(result, Completed)
    assert result.reply == "Order AB-12345 is currently shipped. It is on its way."
    assert llm.calls == []


@pytest.mark.discharges("P-DIRECT", "AHC-0100")
async def test_an_ambiguous_turn_reaches_the_loop(server) -> None:
    llm = ScriptedClient([calls("get_order", order_id="AB-1"), says("It has shipped.")])
    async with open_tools(server) as tools:
        result, _ = await agent_with(tools, llm=llm).handle(
            "cancel one of my orders", identity=customer()
        )
    assert isinstance(result, Completed)
    assert result.reply == "It has shipped."
    assert len(llm.calls) == 2


@pytest.mark.discharges("AAC-0009", "AHC-0005", "AHC-0017", "B11")
async def test_provider_failure_returns_a_typed_result(server) -> None:
    async with open_tools(server) as tools:
        result, _ = await agent_with(tools, llm=UnavailableClient()).handle(
            "something ambiguous", identity=customer()
        )
    assert isinstance(result, Failed)


# --------------------------------------------------------------------------- #
# Conversation. What the customer can be shown.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("fact:turn_count", "AHC-0044")
async def test_the_turn_is_recorded_on_the_conversation(server) -> None:
    async with open_tools(server) as tools:
        _, conversation = await agent_with(tools).handle(
            "can I get a discount?", identity=customer()
        )
    roles = [m.role for m in conversation.messages]
    assert roles == ["user", "assistant"]
    assert conversation.customer_id == "C-1042"


@pytest.mark.discharges("AAC-0006")
async def test_operator_detail_never_reaches_the_conversation(server) -> None:
    """`Failed.detail` is for the operator and lives on the span. A conversation
    is what the customer can be shown."""
    async with open_tools(server) as tools:
        result, conversation = await agent_with(tools, llm=UnavailableClient()).handle(
            "something ambiguous", identity=customer()
        )
    assert isinstance(result, Failed)
    assert result.detail not in conversation.messages[-1].content


@pytest.mark.discharges("AHC-0044")
async def test_history_carries_into_the_next_turn(server) -> None:
    llm = ScriptedClient([says("first"), says("second")])
    async with open_tools(server) as tools:
        agent = agent_with(tools, llm=llm)
        _, conversation = await agent.handle("tell me something", identity=customer())
        _, conversation = await agent.handle(
            "and something else", identity=customer(), conversation=conversation
        )
    assert len(conversation.messages) == 4
    assert "tell me something" in llm.calls[1].messages[1].content


# --------------------------------------------------------------------------- #
# Checkpointing. P6 exists because enforcement must survive process death.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0044")
async def test_a_turn_is_checkpointed(server) -> None:
    store = InMemoryCheckpointStore()
    run_id = RunId("run_fixed")
    async with open_tools(server) as tools:
        await agent_with(tools, store=store).handle(
            "can I get a discount?", identity=customer(), run_id=run_id
        )
    raw = await store.resume(run_id)
    assert raw is not None
    assert Conversation.decode(raw).customer_id == "C-1042"


DURABILITY_CASES = [
    ("in memory", InMemoryCheckpointStore, False),
    ("file backed", FileCheckpointStore, True),
]


@pytest.mark.parametrize(
    ("name", "cls", "durable"), DURABILITY_CASES, ids=[c[0] for c in DURABILITY_CASES]
)
@pytest.mark.discharges("AHC-0102")
def test_each_store_declares_its_durability(name: str, cls: type, durable: bool) -> None:
    """Stated per implementation rather than assumed. An approval may take a
    human an hour, and a store that dies with the process is absent exactly when
    it was needed."""
    assert cls.durable is durable


@pytest.mark.discharges("AHC-0044")
async def test_a_file_checkpoint_survives_a_new_store_instance(tmp_path) -> None:
    """The closest thing to a restart a test can stage."""
    run_id = RunId("run_restart")
    conversation = Conversation(
        conversation_id=ConversationId("cnv_1"),
        customer_id="C-1042",
        messages=(Message(role="user", content="hello"),),
    )
    await FileCheckpointStore(tmp_path).checkpoint(
        run_id, conversation.encode(), conversation_id=conversation.conversation_id
    )

    recovered = await FileCheckpointStore(tmp_path).resume(run_id)
    assert recovered is not None
    assert Conversation.decode(recovered).messages[0].content == "hello"


@pytest.mark.discharges("AHC-0102")
async def test_a_truncated_checkpoint_is_refused_rather_than_resumed(tmp_path) -> None:
    """A checkpoint half-written during a crash is worse than none — it resumes
    into a state that never existed."""
    store = FileCheckpointStore(tmp_path)
    (tmp_path / "run_broken.json").write_bytes(b'{"conversation_id": "cnv_1", "cust')
    assert await store.resume(RunId("run_broken")) is None


@pytest.mark.discharges("AHC-0102")
async def test_resuming_an_unknown_run_is_none_not_an_error(tmp_path) -> None:
    assert await FileCheckpointStore(tmp_path).resume(RunId("never_ran")) is None


# --------------------------------------------------------------------------- #
# The trace carries what makes a verdict interpretable.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0011", "AAC-0107", "AHC-0003", "B8")
async def test_the_turn_span_carries_the_config_fingerprint(server, exporter) -> None:
    config = resolve(Settings(provider_api_key="k", resolution="mock", sealed=True))
    async with open_tools(server) as tools:
        await agent_with(tools, config=config).handle("can I get a discount?", identity=customer())
    turn = next(s for s in exporter.get_finished_spans() if s.name == "agent.turn")
    attrs = tel.attributes_of(turn)
    assert attrs[tel.CONFIG_FINGERPRINT] == config.fingerprint
    assert attrs[tel.RESOLUTION] == "mock"


@pytest.mark.discharges("AAC-0100", "B8")
async def test_a_direct_route_is_visible_on_the_trace(server, exporter) -> None:
    """A verdict that says "no model call" should be checkable without trusting
    the reply text."""
    async with open_tools(server) as tools:
        await agent_with(tools, llm=ScriptedClient([])).handle(
            "where is my order AB-12345", identity=customer()
        )
    names = [s.name for s in exporter.get_finished_spans()]
    assert "agent.direct" in names
    assert "gen_ai.chat" not in names
