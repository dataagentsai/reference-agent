"""The Azure stack's model layer: Pydantic AI's model requests, behind our port.

T-099. `stacks/azure.yaml` binds the model to the APIM AI gateway and names
Pydantic AI as the layer the call goes through (`x_model_layer:
pydantic-ai-direct`) — **the model layer, never the loop**. So this is one more
`LLMClient`, exactly as `GroqClient` is: the loop, the budgets, the retries and
the span contract do not know which one they were handed.

**The direct API only.** `pydantic_ai.direct.model_request` is one request and
one response with the schema translated; no agent, no graph, no tool execution,
no retries. Pydantic AI's own instrumentation is off: the `gen_ai.chat` span and
the GenAI client metrics are emitted here, by the same names `GroqClient` uses,
so a trace does not say which layer made the call except by its provider name.

**Two routes, one door.** An OpenAI-compatible endpoint with a key — Groq
directly, or APIM in front of Groq with the subscription key in the header APIM
reads (`api-key` or `Ocp-Apim-Subscription-Key`) — and Azure OpenAI behind APIM.
Foundry is a backend change in APIM, not here (the stack file's `x_dev`). The
SDK client is built here with **no retries of its own**, for the reason
`GroqClient` gives: a retry inside the SDK is invisible to `ResilientLLM`.

**Failures by kind** (`contracts.failures`): the same mapping as `failure_from`.

Only this module may import `pydantic_ai` (import contract); it is the optional
`agent-harness[azure]` dependency, imported only by a composition root.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any, cast

from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai import messages as pa
from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from agent_harness import telemetry as tel
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
from agent_harness.llm import RETRYABLE_4XX, ModelChoice
from agent_harness.llm.served import provider_from_host

AZURE_API_VERSION = "2024-10-21"
"""The Azure OpenAI data-plane version (GA) asked for when a caller names none."""

GATEWAY_KEY_HEADERS = frozenset({"api-key", "Ocp-Apim-Subscription-Key"})
"""Where APIM reads a subscription key: its own header, or the one it imports
Azure OpenAI APIs with. Nothing else is accepted, so a typo fails at startup."""

_GATEWAY_PLACEHOLDER = "behind-the-gateway"
"""What the SDK's `Authorization` carries when the key travels in a gateway
header: the SDK refuses an empty key, and the gateway sets the backend's own."""


def to_messages(messages: Iterable[Message]) -> list[pa.ModelMessage]:
    """Our transcript as Pydantic AI's: requests and responses, alternating.

    Consecutive system, user and tool messages are parts of one request; an
    assistant turn is a response carrying its text and the calls it made, so a
    tool result can refer to the call id that produced it. Provenance is ours and
    does not go on the wire, as in `GroqClient`.
    """
    out: list[pa.ModelMessage] = []
    parts: list[pa.ModelRequestPart] = []
    for m in messages:
        if m.role == "assistant":
            if parts:
                out.append(pa.ModelRequest(parts=parts))
                parts = []
            said: list[pa.ModelResponsePart] = [pa.TextPart(m.content)] if m.content else []
            said += [
                pa.ToolCallPart(c.name, dict(c.arguments), tool_call_id=c.id) for c in m.tool_calls
            ]
            out.append(pa.ModelResponse(parts=said))
        elif m.role == "system":
            parts.append(pa.SystemPromptPart(m.content))
        elif m.role == "user":
            parts.append(pa.UserPromptPart(m.content))
        else:
            parts.append(
                pa.ToolReturnPart(m.tool_name or "", m.content, tool_call_id=m.tool_call_id or "")
            )
    if parts:
        out.append(pa.ModelRequest(parts=parts))
    return out


def to_tools(tools: Iterable[Mapping[str, object]]) -> list[ToolDefinition]:
    """`context`'s OpenAI-shaped tool definitions as Pydantic AI's.

    Never strict: strict mode rewrites the schema the model is shown, and the
    schema is the tool's own (`ToolSpec.input_schema`), the same as the other
    client sends. A definition of another shape is this build's mistake, not the
    model's, and fails before any call is made.
    """
    out = []
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, Mapping):
            raise ValueError(f"not a function tool definition: {tool!r}")
        out.append(
            ToolDefinition(
                name=str(function["name"]),
                description=str(function.get("description") or ""),
                parameters_json_schema=dict(
                    cast(Mapping[str, Any], function.get("parameters") or {})
                ),
                strict=False,
            )
        )
    return out


def from_response(response: pa.ModelResponse) -> ModelResponse:
    """The typed boundary (AHC-0001), on Pydantic AI's parsed response.

    The same two outcomes as `_from_wire`: a response, or `ModelMalformed` for
    tool arguments that are not a JSON object and for a paid-for answer with
    neither text nor a call (F-031).
    """
    calls: list[ToolCall] = []
    for part in response.parts:
        if not isinstance(part, pa.ToolCallPart):
            continue
        try:
            arguments = part.args_as_dict(raise_if_invalid=True)
        except (ValueError, AssertionError) as exc:
            raise ModelMalformed(
                f"tool call {part.tool_name!r} has unreadable arguments: {exc}",
                raw=str(part.args),
            ) from exc
        calls.append(ToolCall(id=part.tool_call_id, name=part.tool_name, arguments=arguments))
    text = "".join(p.content for p in response.parts if isinstance(p, pa.TextPart))
    usage = response.usage
    if not text.strip() and not calls:
        raise ModelMalformed(
            "the model returned neither text nor a tool call after "
            f"{usage.output_tokens} output tokens"
            + (
                " — the output budget was spent before an answer began"
                if response.finish_reason == "length"
                else ""
            ),
            raw=text,
        )
    # The provider's own word for why it stopped, where it gave one, so the loop
    # reads `length` and `tool_calls` as it does from the other client.
    details = response.provider_details or {}
    return ModelResponse(
        text=text,
        tool_calls=tuple(calls),
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cache_read_tokens,
        ),
        model=response.model_name or "",
        stop_reason=str(details.get("finish_reason") or response.finish_reason or ""),
    )


def failure_of(error: Exception) -> ModelUnavailable | ModelMalformed:
    """What a Pydantic AI error means for the caller, by kind — `failure_from`'s
    rules, read off Pydantic AI's exceptions instead of the SDK's."""
    if isinstance(error, ContentFilterError):
        return ModelRefused(str(error))
    if isinstance(error, UnexpectedModelBehavior):
        return ModelMalformed(str(error), raw=str(error.body or ""))
    if not isinstance(error, ModelHTTPError):
        return ModelUnavailable(str(error))  # unreachable, timed out: no status at all
    status = error.status_code
    if status == 429:
        if _error_type(error.body) == "budget_exceeded":
            return ModelBudgetExhausted(str(error))
        return ModelThrottled(str(error), retry_after=_retry_after(error.headers))
    if 400 <= status < 500 and status not in RETRYABLE_4XX:
        return ModelRefused(str(error))
    return ModelUnavailable(str(error))


def _error_type(body: object) -> object:
    inner = body.get("error", body) if isinstance(body, dict) else None
    return inner.get("type") if isinstance(inner, dict) else None


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    raw = (headers or {}).get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


class PydanticAIClient:
    """`real` resolution through Pydantic AI's model layer. An `LLMClient`.

    Built from a Pydantic AI `Model` — any of them: the composition root's
    factories below for a live endpoint, a `FunctionModel` or `TestModel` in a
    test. `base_url` is only for `served_provider`.
    """

    def __init__(
        self,
        model: Model,
        *,
        provider: str,
        base_url: str = "",
        temperature: float = 0.0,
    ) -> None:
        self._model = model
        self._provider = provider
        self._base_url = base_url
        self._temperature = temperature

    async def complete(self, request: ModelRequest) -> ModelResponse:
        temperature = self._temperature if request.temperature is None else request.temperature
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: self._provider,
                tel.GEN_AI_SYSTEM: self._provider,  # deprecated; emitted during migration
                tel.GEN_AI_OPERATION: "chat",
                tel.GEN_AI_REQUEST_MODEL: self._model.model_name,
            },
        ) as span:
            span.set_attribute("gen_ai.request.temperature", temperature)
            standard = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider,
                "gen_ai.request.model": self._model.model_name,
            }
            started = time.monotonic()
            try:
                raw = await model_request(
                    self._model,
                    to_messages(request.messages),
                    model_settings=ModelSettings(
                        max_tokens=request.max_tokens, temperature=temperature
                    ),
                    model_request_parameters=ModelRequestParameters(
                        function_tools=to_tools(request.tools), allow_text_output=True
                    ),
                    instrument=False,
                )
            except (ModelAPIError, UnexpectedModelBehavior) as exc:
                failure = failure_of(exc)
                tel.counters.operation_duration.record(
                    time.monotonic() - started, standard | {"error.type": type(failure).__name__}
                )
                raise failure from exc
            response = from_response(raw)
            _record(span, standard, response, time.monotonic() - started)
            return response

    async def served_provider(self) -> str | None:
        """Who serves the model, as far as the route says. A provider's own
        endpoint is recognised by its host; a gateway (APIM) has no `/model/info`
        and its host says nothing, so the answer is `None`, unverified — the
        gateway's configuration is where the backend is decided."""
        return provider_from_host(self._base_url) if self._base_url else None


def _record(span: Any, standard: dict[str, str], response: ModelResponse, took: float) -> None:
    standard = standard | {"gen_ai.response.model": response.model}
    tel.counters.operation_duration.record(took, standard)
    for kind, count in (
        ("input", response.usage.input_tokens),
        ("output", response.usage.output_tokens),
    ):
        tel.counters.token_usage.record(
            count, standard | {"gen_ai.token.type": kind, "config": tel.counters.configuration()}
        )
    span.set_attribute(tel.GEN_AI_RESPONSE_MODEL, response.model)
    tel.set_usage(
        span, input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
    )


def _key_headers(api_key: str, key_header: str | None) -> dict[str, str]:
    if key_header is None:
        return {}
    if key_header not in GATEWAY_KEY_HEADERS:
        raise ValueError(f"{key_header!r} is not a header APIM reads a key from")
    return {key_header: api_key}


def openai_compatible(
    *,
    api_key: str,
    base_url: str,
    model: str,
    provider: str = "groq",
    temperature: float = 0.0,
    key_header: str | None = None,
    timeout_s: float = 60.0,
    http_client: Any = None,
) -> PydanticAIClient:
    """Groq directly, or APIM in front of it: any OpenAI-compatible endpoint.

    With `key_header`, the key is APIM's subscription key and travels in that
    header only; the gateway puts the backend's own key on the call it forwards.
    """
    headers = _key_headers(api_key, key_header)
    sdk = AsyncOpenAI(
        api_key=_GATEWAY_PLACEHOLDER if headers else api_key,
        base_url=base_url,
        default_headers=headers or None,
        max_retries=0,
        timeout=timeout_s,
        http_client=http_client,
    )
    chat = OpenAIChatModel(model, provider=OpenAIProvider(openai_client=sdk))
    return PydanticAIClient(chat, provider=provider, base_url=base_url, temperature=temperature)


def azure_openai(
    *,
    api_key: str,
    endpoint: str,
    deployment: str,
    api_version: str = AZURE_API_VERSION,
    provider: str = "azure.ai.openai",
    temperature: float = 0.0,
    key_header: str | None = None,
    timeout_s: float = 60.0,
    http_client: Any = None,
) -> PydanticAIClient:
    """Azure OpenAI through APIM: `endpoint` is the gateway's, `deployment` the
    model deployment it routes to. The SDK sends the key as `api-key`, which is
    the header APIM imports an Azure OpenAI API with; `key_header` names another."""
    sdk = AsyncAzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version,
        default_headers=_key_headers(api_key, key_header) or None,
        max_retries=0,
        timeout=timeout_s,
        http_client=http_client,
    )
    chat = OpenAIChatModel(deployment, provider=AzureProvider(openai_client=sdk))
    return PydanticAIClient(chat, provider=provider, base_url="", temperature=temperature)


AZURE_PROVIDERS = frozenset({"azure", "azure.ai.openai"})
"""Declared providers that mean Azure OpenAI's own API shape, not OpenAI's."""


async def connect(
    config: ModelChoice, *, api_key: str, key_header: str | None = None
) -> tuple[PydanticAIClient, bool]:
    """`connect_model` on this layer: the client, and whether the declared
    provider was verified against the route (T-018)."""
    client = from_choice(config, api_key=api_key, key_header=key_header)
    return client, config.check_served_by(await client.served_provider())


def from_choice(
    config: ModelChoice, *, api_key: str, key_header: str | None = None
) -> PydanticAIClient:
    """The client a resolved configuration names, on this layer."""
    if config.provider in AZURE_PROVIDERS:
        return azure_openai(
            api_key=api_key,
            endpoint=config.provider_base_url,
            deployment=config.model,
            provider=config.provider,
            temperature=config.temperature,
            key_header=key_header,
        )
    return openai_compatible(
        api_key=api_key,
        base_url=config.provider_base_url,
        model=config.model,
        provider=config.provider,
        temperature=config.temperature,
        key_header=key_header,
    )


__all__ = [
    "AZURE_API_VERSION",
    "AZURE_PROVIDERS",
    "GATEWAY_KEY_HEADERS",
    "PydanticAIClient",
    "azure_openai",
    "connect",
    "failure_of",
    "from_choice",
    "from_response",
    "openai_compatible",
    "to_messages",
    "to_tools",
]
