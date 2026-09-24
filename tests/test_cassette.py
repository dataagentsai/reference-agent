"""Record and replay — AgentTwin's `replay` mode.

The test that matters is the last one: a whole agent run recorded once, then
replayed byte-identically from disk with a client that cannot reach a network
even if it wanted to.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import telemetry as tel
from support_agent.cassette import (
    FORMAT_VERSION,
    Cassette,
    CassetteMiss,
    Match,
    Player,
    Recorder,
    fingerprint,
)
from support_agent.contracts import (
    Identity,
    LLMClient,
    Message,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    SideEffectClass,
    ToolCall,
    Usage,
)
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.tools import META_SIDE_EFFECT, connect


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


def request(text: str = "hello", **overrides) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),), **overrides)


def says(text: str) -> ModelResponse:
    return ModelResponse(text=text, usage=Usage(input_tokens=7, output_tokens=3))


# --------------------------------------------------------------------------- #
# Fingerprinting. What counts as the same question.
# --------------------------------------------------------------------------- #

SAME_CASES = [
    ("identical", request(), request(), True),
    ("different message", request("a"), request("b"), False),
    ("different temperature", request(), request(temperature=0.7), False),
    ("different max_tokens", request(), request(max_tokens=99), False),
]


@pytest.mark.parametrize(
    ("name", "left", "right", "same"), SAME_CASES, ids=[c[0] for c in SAME_CASES]
)
@pytest.mark.discharges("AHC-0105")
def test_fingerprint_identity(
    name: str, left: ModelRequest, right: ModelRequest, same: bool
) -> None:
    assert (fingerprint(left) == fingerprint(right)) is same


@pytest.mark.discharges("AHC-0105")
def test_rewording_a_tool_description_does_not_invalidate_a_cassette() -> None:
    """Only tool *names* count. A docstring edit is not a different question, and
    including schemas would invalidate every recording on a comment change."""

    def tools(description: str):
        return (
            {"type": "function", "function": {"name": "get_order", "description": description}},
        )

    assert fingerprint(request(tools=tools("Look up an order."))) == fingerprint(
        request(tools=tools("Fetch an order by its id."))
    )


@pytest.mark.discharges("AHC-0105")
def test_adding_a_tool_does_invalidate_a_cassette() -> None:
    """A different action surface is a different question."""
    one = ({"type": "function", "function": {"name": "get_order"}},)
    two = (*one, {"type": "function", "function": {"name": "issue_refund"}})
    assert fingerprint(request(tools=one)) != fingerprint(request(tools=two))


# --------------------------------------------------------------------------- #
# Recording is transparent.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0105")
async def test_recording_does_not_change_behaviour() -> None:
    inner = ScriptedClient([says("one"), says("two")])
    recorder = Recorder(inner)
    assert (await recorder.complete(request("a"))).text == "one"
    assert (await recorder.complete(request("b"))).text == "two"
    assert len(recorder.cassette) == 2


@pytest.mark.discharges("AHC-0022")
def test_a_recorder_satisfies_the_client_protocol() -> None:
    assert isinstance(Recorder(ScriptedClient([])), LLMClient)
    assert isinstance(Player(Cassette()), LLMClient)


# --------------------------------------------------------------------------- #
# Replay. A miss is an error, never a network call.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("B5")
async def test_a_player_cannot_reach_a_provider_even_on_a_miss() -> None:
    """No fallback client exists on `Player`, so "this suite makes no calls" is a
    property of the type rather than a promise in a docstring."""
    player = Player(Cassette())
    with pytest.raises(CassetteMiss):
        await player.complete(request())
    assert not hasattr(player, "_inner")


@pytest.mark.discharges("AHC-0105")
async def test_a_changed_request_is_a_mismatch_not_a_silent_pass() -> None:
    """The drift a regression case exists to catch."""
    recorder = Recorder(ScriptedClient([says("recorded")]))
    await recorder.complete(request("original question"))

    player = Player(recorder.cassette)
    with pytest.raises(CassetteMiss, match="something changed"):
        await player.complete(request("a different question"))


@pytest.mark.discharges("AHC-0105")
async def test_a_run_longer_than_its_recording_is_a_miss() -> None:
    recorder = Recorder(ScriptedClient([says("only one")]))
    await recorder.complete(request("a"))

    player = Player(recorder.cassette)
    await player.complete(request("a"))
    with pytest.raises(CassetteMiss, match="exhausted"):
        await player.complete(request("a"))


@pytest.mark.discharges("AHC-0105")
async def test_by_request_matching_tolerates_reordering() -> None:
    recorder = Recorder(ScriptedClient([says("first"), says("second")]))
    await recorder.complete(request("a"))
    await recorder.complete(request("b"))

    player = Player(recorder.cassette, match=Match.BY_REQUEST)
    assert (await player.complete(request("b"))).text == "second"
    assert (await player.complete(request("a"))).text == "first"


@pytest.mark.discharges("AHC-0105")
async def test_ordered_matching_does_not_tolerate_reordering() -> None:
    """Which is the point — it is the default for exactly this reason."""
    recorder = Recorder(ScriptedClient([says("first"), says("second")]))
    await recorder.complete(request("a"))
    await recorder.complete(request("b"))

    with pytest.raises(CassetteMiss):
        await Player(recorder.cassette).complete(request("b"))


@pytest.mark.discharges("AHC-0105")
def test_a_cassette_miss_is_not_a_provider_outage() -> None:
    """They must stay distinct: a provider being down is a production condition
    to degrade through; a missing recording is a test defect that must fail
    loudly rather than be absorbed by a degradation path."""
    assert not issubclass(CassetteMiss, ModelUnavailable)


# --------------------------------------------------------------------------- #
# On disk.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
async def test_a_cassette_round_trips_through_a_file(tmp_path) -> None:
    recorder = Recorder(
        ScriptedClient(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="tc", name="get_order", arguments={"order_id": "AB-1"}),
                    ),
                    usage=Usage(input_tokens=11, output_tokens=4),
                ),
                says("It has shipped."),
            ]
        )
    )
    await recorder.complete(request("a"))
    await recorder.complete(request("b"))

    path = tmp_path / "run.json"
    recorder.cassette.save(path)
    loaded = Cassette.load(path)

    assert len(loaded) == 2
    assert loaded.exchanges[0].response.tool_calls[0].arguments == {"order_id": "AB-1"}
    assert loaded.exchanges[1].response.usage.input_tokens == 7


@pytest.mark.tooling
def test_an_unknown_format_is_refused_rather_than_reinterpreted(tmp_path) -> None:
    path = tmp_path / "old.json"
    path.write_text('{"format": 99, "exchanges": []}')
    with pytest.raises(ValueError, match="re-record"):
        Cassette.load(path)


@pytest.mark.tooling
def test_the_format_version_is_pinned() -> None:
    """1 → 2 when a cassette gained the configuration it was recorded under
    (AAC-0096). Bumped deliberately: a version-1 file has no context, so loading
    one under the new rules would silently look like a recording that declared
    nothing, which is exactly the state the gate exists to refuse."""
    assert FORMAT_VERSION == 2


# --------------------------------------------------------------------------- #
# The one that matters: a whole run, recorded once, replayed from disk.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0059", "AHC-0022")
async def test_a_whole_run_replays_from_disk_with_no_network(server, tmp_path) -> None:
    scripted = ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="tc", name="get_order", arguments={"order_id": "AB-1"}),),
                usage=Usage(input_tokens=20, output_tokens=6),
            ),
            says("Your order has shipped."),
        ]
    )
    recorder = Recorder(scripted)

    async with connect(server, requests=InMemoryRequests()) as tools:
        live_result, live_trace = await agent_loop.run(
            "where is AB-1",
            identity=customer(),
            llm=recorder,
            tools=tools,
            system_prompt="You are support.",
            run_id="run_fixed",  # type: ignore[arg-type]
        )

    path = tmp_path / "cassette.json"
    recorder.cassette.save(path)

    player = Player(Cassette.load(path))
    async with connect(server, requests=InMemoryRequests()) as tools:
        replayed_result, replayed_trace = await agent_loop.run(
            "where is AB-1",
            identity=customer(),
            llm=player,
            tools=tools,
            system_prompt="You are support.",
            run_id="run_fixed",  # type: ignore[arg-type]
        )

    assert replayed_result == live_result
    assert replayed_trace.steps == live_trace.steps
    assert replayed_trace.usage == live_trace.usage
    assert replayed_trace.tool_calls == live_trace.tool_calls
    assert player.exhausted


@pytest.mark.discharges("AAC-0100", "AHC-0027")
async def test_replay_is_visible_on_the_trace(server, exporter, tmp_path) -> None:
    """A verdict is not interpretable without knowing which world produced it."""
    recorder = Recorder(ScriptedClient([says("hi")]))
    await recorder.complete(request("a"))

    await Player(recorder.cassette).complete(request("a"))
    chat = [s for s in exporter.get_finished_spans() if s.name == "gen_ai.chat"][-1]
    attrs = tel.attributes_of(chat)
    assert attrs[tel.RESOLUTION] == "replay"
    assert attrs[tel.GEN_AI_SYSTEM] == "cassette"
