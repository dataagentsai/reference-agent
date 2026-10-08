"""T-099 — `PydanticAIClient`: the Azure stack's model layer behind our `LLMClient`.

Three tables, no network. The first runs the port's types through Pydantic AI's
`FunctionModel` and back: messages, tool definitions, tool calls, usage, and the
two malformed answers. The second puts an `httpx2.MockTransport` under the real
OpenAI provider and checks what goes on the wire for each route — Groq directly,
APIM in front of Groq, Azure OpenAI behind APIM. The third scripts the
transport's failures and holds each to its kind, then runs them through
`ResilientLLM`, which must keep retrying what is transient and nothing else.

What only a live call can show — that APIM's policy swaps the subscription key
for the backend's, and that Groq and Azure accept the request as sent — is not
here, and says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from pydantic_ai import messages as pa
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_harness.contracts import (
    Message,
    ModelBudgetExhausted,
    ModelMalformed,
    ModelRefused,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
    ToolCall,
    Usage,
)
from agent_harness.llm import connect_model
from agent_harness.llm.pydantic_ai import (
    PydanticAIClient,
    azure_openai,
    openai_compatible,
    to_tools,
)
from agent_harness.resilience import Backoff, CircuitBreaker, ResilientLLM

TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_order",
        "description": "Find an order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}
CALL = ToolCall(id="call_1", name="lookup_order", arguments={"order_id": "A-1001"})
TRANSCRIPT = (
    Message(role="system", content="You help with orders."),
    Message(role="user", content="Where is A-1001?"),
    Message(role="assistant", content="", tool_calls=(CALL,)),
    Message(
        role="tool",
        content='{"status": "shipped"}',
        tool_call_id="call_1",
        tool_name="lookup_order",
        provenance="tool",
    ),
)
REQUEST = ModelRequest(messages=TRANSCRIPT, tools=(TOOL,), max_tokens=256, temperature=0.3)
USAGE = RequestUsage(input_tokens=120, output_tokens=9, cache_read_tokens=64)


def answering(*parts: pa.ModelResponsePart, finish: Any = "stop") -> tuple[FunctionModel, dict]:
    """A FunctionModel that answers with `parts` and keeps what it was sent."""
    seen: dict[str, Any] = {}

    def respond(messages: list[pa.ModelMessage], info: AgentInfo) -> pa.ModelResponse:
        seen["messages"], seen["info"] = messages, info
        return pa.ModelResponse(
            parts=list(parts), usage=USAGE, model_name="fn-model", finish_reason=finish
        )

    return FunctionModel(respond, model_name="fn-model"), seen


# (name, the model's answer parts, finish reason, expected response or failure type)
ANSWERS: list[tuple[str, list[pa.ModelResponsePart], Any, ModelResponse | type[Exception]]] = [
    (
        "text, with usage and cached tokens",
        [pa.TextPart("It shipped yesterday.")],
        "stop",
        ModelResponse(
            text="It shipped yesterday.",
            usage=Usage(input_tokens=120, output_tokens=9, cached_input_tokens=64),
            model="fn-model",
            stop_reason="stop",
        ),
    ),
    (
        "a tool call with object arguments",
        [pa.ToolCallPart("lookup_order", {"order_id": "A-1001"}, tool_call_id="call_2")],
        "tool_call",
        ModelResponse(
            tool_calls=(
                ToolCall(id="call_2", name="lookup_order", arguments={"order_id": "A-1001"}),
            ),
            usage=Usage(input_tokens=120, output_tokens=9, cached_input_tokens=64),
            model="fn-model",
            stop_reason="tool_call",
        ),
    ),
    (
        "a tool call with JSON-string arguments is parsed",
        [
            pa.TextPart("Checking."),
            pa.ToolCallPart("lookup_order", '{"order_id": "A-7"}', tool_call_id="call_3"),
        ],
        "tool_call",
        ModelResponse(
            text="Checking.",
            tool_calls=(ToolCall(id="call_3", name="lookup_order", arguments={"order_id": "A-7"}),),
            usage=Usage(input_tokens=120, output_tokens=9, cached_input_tokens=64),
            model="fn-model",
            stop_reason="tool_call",
        ),
    ),
    (
        "truncated arguments are malformed",
        [pa.ToolCallPart("lookup_order", '{"order_id": "A-', tool_call_id="c")],
        "length",
        ModelMalformed,
    ),
    (
        "arguments that are not an object are malformed",
        [pa.ToolCallPart("lookup_order", "[1, 2]", tool_call_id="c")],
        "tool_call",
        ModelMalformed,
    ),
    ("neither text nor a call is malformed (F-031)", [pa.TextPart("  ")], "length", ModelMalformed),
]


@pytest.mark.parametrize(
    ("name", "parts", "finish", "expected"), ANSWERS, ids=[a[0] for a in ANSWERS]
)
async def test_the_port_round_trips_through_pydantic_ai(
    name: str,
    parts: list[pa.ModelResponsePart],
    finish: Any,
    expected: ModelResponse | type[Exception],
) -> None:
    model, seen = answering(*parts, finish=finish)
    client = PydanticAIClient(model, provider="groq", temperature=0.0)
    if isinstance(expected, ModelResponse):
        assert await client.complete(REQUEST) == expected
    else:
        with pytest.raises(expected):
            await client.complete(REQUEST)

    # Whatever came back, what went out was the transcript, the tools and the
    # sampling parameters, translated and nothing more.
    first, said, results = seen["messages"]
    assert [type(p) for p in first.parts] == [pa.SystemPromptPart, pa.UserPromptPart]
    assert [p.content for p in first.parts] == ["You help with orders.", "Where is A-1001?"]
    (call,) = said.parts
    assert (call.tool_name, call.args, call.tool_call_id) == (
        "lookup_order",
        {"order_id": "A-1001"},
        "call_1",
    )
    (result,) = results.parts
    assert (result.tool_name, result.content, result.tool_call_id) == (
        "lookup_order",
        '{"status": "shipped"}',
        "call_1",
    )
    info: AgentInfo = seen["info"]
    (tool,) = info.function_tools
    assert (tool.name, tool.description, tool.parameters_json_schema, tool.strict) == (
        "lookup_order",
        "Find an order.",
        TOOL["function"]["parameters"],
        False,
    )
    assert info.model_settings == {"max_tokens": 256, "temperature": 0.3}


def test_a_tool_of_another_shape_fails_before_any_call() -> None:
    with pytest.raises(ValueError, match="not a function tool"):
        to_tools([{"type": "web_search"}])


CHAT_OK = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "openai/gpt-oss-120b",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "lookup_order", "arguments": '{"order_id": "A-9"}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57},
}


def transport(*answers: httpx2.Response | Exception) -> tuple[httpx2.AsyncClient, list]:
    """An HTTP client whose every request gets the next scripted answer."""
    sent: list[httpx2.Request] = []
    script = list(answers)

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle)), sent


def groq(http: httpx2.AsyncClient) -> PydanticAIClient:
    return openai_compatible(
        api_key="gsk-direct",
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        http_client=http,
    )


def apim_groq(http: httpx2.AsyncClient) -> PydanticAIClient:
    return openai_compatible(
        api_key="apim-sub",
        base_url="https://gw.azure-api.net/groq/v1",
        model="openai/gpt-oss-120b",
        key_header="Ocp-Apim-Subscription-Key",
        http_client=http,
    )


def apim_azure(http: httpx2.AsyncClient) -> PydanticAIClient:
    return azure_openai(
        api_key="apim-sub",
        endpoint="https://gw.azure-api.net",
        deployment="gpt-4o-mini",
        http_client=http,
    )


# (name, client factory, URL called, headers that must be sent, headers that must not)
ROUTES: list[tuple[str, Any, str, dict[str, str], set[str]]] = [
    (
        "Groq directly: the key as a bearer token",
        groq,
        "https://api.groq.com/openai/v1/chat/completions",
        {"authorization": "Bearer gsk-direct"},
        {"ocp-apim-subscription-key", "api-key"},
    ),
    (
        "APIM in front of Groq: the subscription key in APIM's header only",
        apim_groq,
        "https://gw.azure-api.net/groq/v1/chat/completions",
        {"ocp-apim-subscription-key": "apim-sub", "authorization": "Bearer behind-the-gateway"},
        {"api-key"},
    ),
    (
        "Azure OpenAI behind APIM: the deployment path and api-key",
        apim_azure,
        "https://gw.azure-api.net/openai/deployments/gpt-4o-mini/chat/completions"
        "?api-version=2024-10-21",
        {"api-key": "apim-sub"},
        {"ocp-apim-subscription-key"},
    ),
]


@pytest.mark.parametrize(
    ("name", "make", "url", "present", "absent"), ROUTES, ids=[r[0] for r in ROUTES]
)
async def test_each_route_puts_the_same_request_on_the_wire(
    name: str, make: Any, url: str, present: dict[str, str], absent: set[str]
) -> None:
    http, sent = transport(httpx2.Response(200, json=CHAT_OK))
    response = await make(http).complete(REQUEST)

    assert response.tool_calls == (
        ToolCall(id="call_9", name="lookup_order", arguments={"order_id": "A-9"}),
    )
    assert (response.usage.input_tokens, response.usage.output_tokens) == (50, 7)
    assert response.stop_reason == "tool_calls"  # the provider's word, as the loop reads it
    (request,) = sent
    assert str(request.url) == url
    for header, value in present.items():
        assert request.headers.get(header) == value
    assert not absent & {h.lower() for h in request.headers}
    body = json.loads(request.content)
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(body["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {
        "order_id": "A-1001"
    }
    assert body["messages"][3]["tool_call_id"] == "call_1"
    assert body["tools"] == [TOOL]  # the tool's own schema, not a strict rewrite of it
    assert body["temperature"] == 0.3
    assert "stream" not in body or body["stream"] is False


def test_an_unknown_gateway_header_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="not a header APIM reads"):
        openai_compatible(api_key="k", base_url="https://gw", model="m", key_header="X-Key")


def error(status: int, body: dict | None = None, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=body or {"error": {"message": "no"}}, headers=headers)


# (name, the transport's answer, failure kind, retry-after, attempts ResilientLLM makes)
FAILURES: list[tuple[str, httpx2.Response | Exception, type[Exception], float | None, int]] = [
    (
        "429 is throttled, with the provider's retry-after",
        error(429, **{"retry-after": "7"}),
        ModelThrottled,
        7.0,
        3,
    ),
    (
        "429 from a spent gateway budget is exhausted",
        error(429, {"error": {"message": "spent", "type": "budget_exceeded"}}),
        ModelBudgetExhausted,
        None,
        1,
    ),
    ("500 is unavailable", error(500), ModelUnavailable, None, 3),
    ("503 is unavailable", error(503), ModelUnavailable, None, 3),
    ("408 is unavailable: not now, rather than no", error(408), ModelUnavailable, None, 3),
    ("a timeout is unavailable", httpx2.ReadTimeout("slow"), ModelUnavailable, None, 3),
    (
        "an unreachable endpoint is unavailable",
        httpx2.ConnectError("down"),
        ModelUnavailable,
        None,
        3,
    ),
    ("401 is refused: a key it does not accept", error(401), ModelRefused, None, 1),
    ("403 is refused", error(403), ModelRefused, None, 1),
    ("400 is refused: a request it will not take", error(400), ModelRefused, None, 1),
    ("404 is refused: no such model or deployment", error(404), ModelRefused, None, 1),
    ("422 is refused", error(422), ModelRefused, None, 1),
    (
        "a 200 that is not a completion is malformed",
        httpx2.Response(200, json={"no": "choices"}),
        ModelMalformed,
        None,
        1,
    ),
]


@pytest.mark.parametrize(
    ("name", "answer", "kind", "retry_after", "attempts"), FAILURES, ids=[f[0] for f in FAILURES]
)
async def test_each_failure_keeps_its_kind_through_resilience(
    name: str,
    answer: httpx2.Response | Exception,
    kind: type[Exception],
    retry_after: float | None,
    attempts: int,
) -> None:
    http, sent = transport(answer)
    with pytest.raises(kind) as raised:
        await groq(http).complete(REQUEST)
    assert type(raised.value) is kind  # the kind exactly, not a parent of it
    assert getattr(raised.value, "retry_after", None) == retry_after
    assert len(sent) == 1  # the SDK made no retry of its own

    async def sleep(_: float) -> None:
        return None

    sent.clear()
    llm = ResilientLLM(
        groq(http),
        backoff=Backoff(base_s=0.0, jitter=0.0),
        breaker=CircuitBreaker(threshold=10, cooldown_s=60),
        sleep=sleep,
    )
    with pytest.raises(kind):
        await llm.complete(REQUEST)
    assert len(sent) == attempts


@dataclass(frozen=True)
class Choice:
    model: str
    provider: str
    provider_base_url: str
    temperature: float = 0.0

    def check_served_by(self, served: str | None) -> bool:
        return served == self.provider


# (name, the choice, the layer, the client expected, whether the provider was verified)
CONNECTIONS: list[tuple[str, Choice, str, str, bool]] = [
    (
        "Groq directly, verified by its host",
        Choice("openai/gpt-oss-120b", "groq", "https://api.groq.com/openai/v1"),
        "pydantic-ai",
        "https://api.groq.com/openai/v1/",
        True,
    ),
    (
        "APIM in front of Groq: unverified, the gateway decides",
        Choice("openai/gpt-oss-120b", "groq", "https://gw.azure-api.net/groq/v1"),
        "pydantic-ai",
        "https://gw.azure-api.net/groq/v1/",
        False,
    ),
    (
        "Azure OpenAI behind APIM",
        Choice("gpt-4o-mini", "azure.ai.openai", "https://gw.azure-api.net"),
        "pydantic-ai",
        "https://gw.azure-api.net/openai/",
        False,
    ),
]


@pytest.mark.parametrize(
    ("name", "choice", "layer", "base_url", "verified"),
    CONNECTIONS,
    ids=[c[0] for c in CONNECTIONS],
)
async def test_connect_model_selects_the_layer(
    name: str, choice: Choice, layer: str, base_url: str, verified: bool
) -> None:
    client, checked = await connect_model(
        choice, api_key="k", layer=layer, key_header="Ocp-Apim-Subscription-Key"
    )
    assert isinstance(client, PydanticAIClient)
    assert checked is verified
    assert str(client._model.client.base_url) == base_url


def test_the_default_layer_is_unchanged() -> None:
    import inspect

    assert inspect.signature(connect_model).parameters["layer"].default == "openai"
