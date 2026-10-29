"""The Anthropic adapter and the loop changes it needed (clinical-data-review-agent F-1..F-5, F-8).

No network and no key: the SDK client is a stand-in that records every request
body and answers from a script, so each property is checked on the exact bytes
that would have been sent.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.config import Budgets
from agent_harness.context import assembled
from agent_harness.contracts import (
    Identity,
    LocalTool,
    Message,
    ModelRefused,
    ModelRequest,
    SideEffectClass,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from agent_harness.llm.anthropic import (
    AnthropicClient,
    messages_to_wire,
    response_from_wire,
    tools_to_wire,
)
from agent_harness.loop import run
from agent_harness.loop.dispatch import _bounded
from agent_harness.loop.spend import account


def block(**fields: Any) -> SimpleNamespace:
    """A content block as the SDK returns it: `to_dict()` gives only what was set."""
    ns = SimpleNamespace(**fields)
    ns.to_dict = lambda: copy.deepcopy(fields)
    return ns


def answer(*blocks: SimpleNamespace, stop: str = "end_turn", **usage: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop,
        stop_details=None,
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=usage.get("input", 100),
            output_tokens=usage.get("output", 20),
            cache_creation_input_tokens=usage.get("cache_write", 0),
            cache_read_input_tokens=usage.get("cache_read", 0),
        ),
    )


class FakeSDK:
    """Records each request body; answers from a script."""

    def __init__(self, script: list[SimpleNamespace]) -> None:
        self.script = list(script)
        self.bodies: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **body: Any) -> SimpleNamespace:
        self.bodies.append(copy.deepcopy(body))
        return self.script.pop(0)


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


# ---- translation, table-driven ------------------------------------------------


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(
    ("transcript", "expected_system", "expected_wire"),
    [
        pytest.param(
            [Message(role="system", content="S"), Message(role="user", content="hi")],
            [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
            [{"role": "user", "content": "hi"}],
            id="leading system becomes the cached system prompt",
        ),
        pytest.param(
            [
                Message(role="user", content="go"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="t1", name="a", arguments={}),),
                    provider_blocks=(
                        THINKING,
                        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                    ),
                ),
            ],
            [],
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        THINKING,
                        {"type": "tool_use", "id": "t1", "name": "a", "input": {}},
                    ],
                },
            ],
            id="provider blocks are sent back verbatim, thinking included",
        ),
        pytest.param(
            [
                Message(role="user", content="go"),
                Message(
                    role="assistant",
                    content="ok",
                    tool_calls=(ToolCall(id="t1", name="a", arguments={"x": 1}),),
                ),
            ],
            [],
            [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "t1", "name": "a", "input": {"x": 1}},
                    ],
                },
            ],
            id="without provider blocks, text plus tool_use",
        ),
        pytest.param(
            [
                Message(role="user", content="go"),
                Message(role="tool", content="r1", tool_call_id="t1", tool_name="a"),
                Message(role="tool", content="r2", tool_call_id="t2", tool_name="b"),
            ],
            [],
            [
                {"role": "user", "content": "go"},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
                        {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
                    ],
                },
            ],
            id="parallel results go back in one message",
        ),
    ],
)
def test_messages_to_wire(transcript, expected_system, expected_wire):
    system, wire = messages_to_wire(transcript)
    assert system == expected_system
    assert wire == expected_wire


@pytest.mark.discharges("AHC-0022")
def test_tools_to_wire_from_the_loops_shape():
    loop_shape = (
        {
            "type": "function",
            "function": {"name": "a", "description": "d", "parameters": {"type": "object"}},
        },
    )
    assert tools_to_wire(loop_shape) == [
        {"name": "a", "description": "d", "input_schema": {"type": "object"}}
    ]


@pytest.mark.discharges("AHC-0001", "AHC-0022")
@pytest.mark.parametrize(
    ("raw", "text", "calls", "stop", "usage"),
    [
        pytest.param(
            answer(
                block(type="thinking", thinking="", signature="s"), block(type="text", text="done")
            ),
            "done",
            0,
            "stop",
            Usage(input_tokens=100, output_tokens=20),
            id="text answer, thinking kept aside",
        ),
        pytest.param(
            answer(block(type="tool_use", id="t", name="a", input={"k": 1}), stop="tool_use"),
            "",
            1,
            "tool_calls",
            Usage(input_tokens=100, output_tokens=20),
            id="a tool call",
        ),
        pytest.param(
            answer(block(type="text", text="cut"), stop="max_tokens"),
            "cut",
            0,
            "length",
            Usage(input_tokens=100, output_tokens=20),
            id="max_tokens is the loop's length",
        ),
        pytest.param(
            answer(block(type="text", text="x"), cache_write=50, cache_read=900),
            "x",
            0,
            "stop",
            Usage(input_tokens=150, output_tokens=20, cached_input_tokens=900),
            id="cache reads are cached input",
        ),
    ],
)
def test_response_from_wire(raw, text, calls, stop, usage):
    r = response_from_wire(raw)
    assert (r.text, len(r.tool_calls), r.stop_reason, r.usage) == (text, calls, stop, usage)
    assert r.provider_blocks == tuple(b.to_dict() for b in raw.content)


@pytest.mark.discharges("AHC-0001")
def test_a_refusal_is_typed_and_names_its_category():
    raw = answer(stop="refusal")
    raw.stop_details = SimpleNamespace(category="bio")
    with pytest.raises(ModelRefused, match="bio"):
        response_from_wire(raw)


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(
    ("effort", "fallbacks", "temperature"),
    [("medium", None, 0.0), (None, "default", 0.7), ("high", None, None)],
)
async def test_request_never_carries_sampling_and_routes_fallbacks(effort, fallbacks, temperature):
    sdk = FakeSDK([answer(block(type="text", text="ok"))])
    client = AnthropicClient(
        model="claude-opus-5-5", effort=effort, fallbacks=fallbacks, client=sdk
    )
    await client.complete(
        ModelRequest(messages=(Message(role="user", content="hi"),), temperature=temperature)
    )
    body = sdk.bodies[0]
    assert "temperature" not in body and "top_p" not in body
    assert body.get("output_config") == ({"effort": effort} if effort else None)
    assert ("fallbacks" in body) is (fallbacks is not None)


# ---- the loop: append-only, verbatim replay, no tool server --------------------


def local_tool(name: str, reply: str) -> LocalTool:
    async def handler(arguments: dict[str, object]) -> ToolResult:
        return ToolResult(name=name, text=reply)

    spec = ToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        side_effect=SideEffectClass.READ,
    )
    return LocalTool(spec=spec, handler=handler)


@pytest.mark.discharges("AHC-0012", "AHC-0022")
async def test_a_job_runs_append_only_and_replays_thinking_verbatim():
    script = [
        answer(
            block(**THINKING),
            block(type="tool_use", id="t1", name="look", input={}),
            stop="tool_use",
        ),
        answer(
            block(**THINKING),
            block(type="tool_use", id="t2", name="look", input={}),
            stop="tool_use",
        ),
        answer(block(type="text", text="finished")),
    ]
    sdk = FakeSDK(script)
    ended = await run(
        "review patient P",
        identity=Identity(customer_id="worker-1"),
        llm=AnthropicClient(model="claude-opus-5-5", client=sdk),
        tools=None,
        local_tools={"look": local_tool("look", "x" * 30_000)},
        system_prompt="You review.",
        budgets=Budgets(max_context_chars=None, max_tool_result_chars=20_000),
    )
    result, _trace = ended
    assert "finished" in str(result)
    assert len(sdk.bodies) == 3
    for earlier, later in zip(sdk.bodies, sdk.bodies[1:], strict=False):
        # Every call sends the previous call's messages unchanged, then more.
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
        assert later["system"] == earlier["system"] and later["tools"] == earlier["tools"]
    first_assistant = sdk.bodies[1]["messages"][1]
    assert first_assistant["role"] == "assistant" and first_assistant["content"][0] == THINKING


@pytest.mark.discharges("AHC-0012")
def test_trimming_still_happens_when_a_limit_is_set():
    history = [
        Message(role="user", content="u" * 10_000),
        Message(role="assistant", content="a" * 10_000),
        Message(role="user", content="u" * 10_000),
        Message(role="assistant", content="a" * 10_000),
        Message(role="user", content="u"),
    ]
    assert assembled(system="s", history=history, max_chars=24_000).trimmed > 0
    assert assembled(system="s", history=history, max_chars=None).trimmed == 0


@pytest.mark.discharges("AHC-0038", "AAC-0105")
@pytest.mark.parametrize(
    ("chars", "limit", "truncated"),
    [(100, 500, False), (5_000, 500, True), (5_000, None, False)],
)
def test_local_results_are_bounded(chars, limit, truncated):
    bounded = _bounded(ToolResult(name="t", text="y" * chars), limit)
    assert bounded.truncated is truncated
    if limit is not None:
        assert len(bounded.for_context()) <= limit


@pytest.mark.discharges("AHC-0101")
def test_cached_tokens_reach_the_run_total():
    total, _, _ = account(
        Usage(input_tokens=1, cached_input_tokens=10),
        Usage(input_tokens=2, cached_input_tokens=20),
        meter=None,
    )
    assert total.cached_input_tokens == 30


async def test_a_run_needs_some_tools():
    with pytest.raises(ValueError, match="tool server, local tools"):
        await run(
            "x",
            identity=Identity(customer_id="w"),
            llm=AnthropicClient(model="m", client=FakeSDK([])),
            tools=None,
            system_prompt="s",
        )


@pytest.mark.discharges("AHC-0024")
def test_the_adapter_never_retries_on_its_own():
    """The SDK retries twice by default; the harness owns retries."""
    client = AnthropicClient(model="claude-opus-5-5", api_key="test-key")
    assert client._client.max_retries == 0
