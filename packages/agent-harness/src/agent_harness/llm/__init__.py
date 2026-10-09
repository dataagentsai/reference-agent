"""How the call is made.

L2 · P3. The single choke point every model call passes through.

The interface is ours, not the provider's. A `base_url` swap on an OpenAI client
reaches Groq, Cerebras, Together, OpenRouter and vLLM, but not Anthropic — whose
content blocks, thinking and cache control do not survive an OpenAI-shaped shim.
So substitutability lives in `LLMClient`, and each provider gets an adapter that
translates at the edge. That is the difference between claiming AHC-0022 and
having it.

The resolution modes from AgentTwin are adapters here, not special cases:
`GroqClient` is `real`, `ScriptedClient` is `mock`. `replay` arrives later as the
cassette, wrapping whichever of these it was recorded from. `PydanticAIClient`
(`llm.pydantic_ai`) is `real` too, on the Azure stack's model layer (T-099).
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterable
from typing import Any, Protocol

from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, RateLimitError

from agent_harness import telemetry as tel
from agent_harness.contracts import (
    LLMClient,
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
from agent_harness.llm.served import PROVIDER_HOSTS, provider_from_host, provider_from_model_info


def _to_wire(messages: Iterable[Message]) -> list[dict[str, object]]:
    """Translate at the edge. Provenance is ours and does not go on the wire —
    it governs how `context` assembles and fences the text, not how the provider
    reads it."""
    wire: list[dict[str, object]] = []
    for m in messages:
        entry: dict[str, object] = {"role": m.role, "content": m.content}
        if m.tool_call_id is not None:
            entry["tool_call_id"] = m.tool_call_id
        if m.tool_name is not None:
            entry["name"] = m.tool_name
        if m.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ]
        wire.append(entry)
    return wire


RETRYABLE_4XX = frozenset({408, 409, 425})
"""Client-error statuses that mean *not now* rather than *no*: a timeout, a
conflict, too early. Every other 4xx is a refusal."""


def failure_from(error: APIError) -> ModelUnavailable:
    """What a provider or gateway error means for the caller, by kind.

    The status alone is not enough. A gateway answers an exhausted budget with
    429, the same status as a rate limit, and says which in the error `type`
    (T-029). The body is read only for that; everything else is the status.
    """
    if isinstance(error, RateLimitError):
        if getattr(error, "type", None) == "budget_exceeded":
            return ModelBudgetExhausted(str(error))
        return ModelThrottled(str(error), retry_after=retry_after_of(error))
    status = error.status_code if isinstance(error, APIStatusError) else None
    if status is not None and 400 <= status < 500 and status not in RETRYABLE_4XX:
        return ModelRefused(str(error))
    return ModelUnavailable(str(error))


def retry_after_of(error: object) -> float | None:
    """Read `retry-after` from a provider error, if it said one.

    Returns `None` rather than a guess. A caller that invents a backoff when the
    provider offered a real one is choosing to be wrong on purpose.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class GroqClient:
    """`real` resolution. Groq over its OpenAI-compatible endpoint.

    Makes **no retries of its own** (`max_retries = 0`): a retry inside the SDK is
    invisible, unattributed and uncounted, which is everything AHC-0024 forbids.
    Retrying, backing off and breaking the circuit are `resilience.ResilientLLM`'s,
    composed around this client at the composition root. A rate limit raises
    `ModelThrottled` with the provider's own `retry-after`; anything else,
    `ModelUnavailable`. A provider error never reaches a customer as a stack trace.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        provider: str = "groq",
        temperature: float = 0.0,
        max_retries: int = 0,
        timeout_s: float = 60.0,
        http_client: Any = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._base_url = base_url
        self._temperature = temperature
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout_s,
            http_client=http_client,  # a test's transport; the SDK's own when None
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: self._provider,
                tel.GEN_AI_SYSTEM: self._provider,  # deprecated; emitted during migration
                tel.GEN_AI_OPERATION: "chat",
                tel.GEN_AI_REQUEST_MODEL: self._model,
            },
        ) as span:
            temperature = self._temperature if request.temperature is None else request.temperature
            span.set_attribute("gen_ai.request.temperature", temperature)
            standard = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider,
                "gen_ai.request.model": self._model,
            }
            started = time.monotonic()
            try:
                raw = await self._client.chat.completions.create(
                    model=self._model,
                    # The two ignores left in the package, both here, at the one
                    # line where our types meet the vendor's. The wire dicts are
                    # built to the SDK's per-role TypedDicts; restating each role
                    # as its own type would re-derive the SDK, not check it.
                    messages=_to_wire(request.messages),  # type: ignore[arg-type]
                    tools=list(request.tools) or None,  # type: ignore[arg-type]
                    max_tokens=request.max_tokens,
                    temperature=temperature,
                )
            except APIError as exc:
                failure = failure_from(exc)
                # The GenAI conventions' own client metric, with `error.type` on
                # a failed call as they specify (T-055).
                tel.counters.operation_duration.record(
                    time.monotonic() - started, standard | {"error.type": type(failure).__name__}
                )
                raise failure from exc

            response = _from_wire(raw)
            standard["gen_ai.response.model"] = response.model
            tel.counters.operation_duration.record(time.monotonic() - started, standard)
            for kind, count in (
                ("input", response.usage.input_tokens),
                ("output", response.usage.output_tokens),
            ):
                # `config`: the turn's configuration, so input tokens per call
                # compare between releases (AACP-0002).
                tel.counters.token_usage.record(
                    count,
                    standard | {"gen_ai.token.type": kind, "config": tel.counters.configuration()},
                )
            span.set_attribute(tel.GEN_AI_RESPONSE_MODEL, response.model)
            tel.set_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response

    async def served_provider(self) -> str | None:
        """Who actually serves this model through `base_url`, asked once at
        startup (T-018). A gateway answers from `/model/info`; a provider's own
        endpoint has no such route and is recognised by its host; anything else
        is `None`, unverified.

        A gateway that cannot be reached fails the process as `ModelUnavailable`:
        starting anyway would defer the same failure to a customer's first turn.
        """
        try:
            payload = await self._client.get("/model/info", cast_to=object)
        except APIStatusError:
            return provider_from_host(self._base_url)
        except APIConnectionError as exc:
            raise ModelUnavailable(f"{self._base_url} is unreachable: {exc}") from exc
        return provider_from_model_info(payload, self._model) or provider_from_host(self._base_url)


class ModelChoice(Protocol):
    """What `connect_model` reads of a resolved configuration.

    An agent's configuration is its own — which models it approves, what it
    fingerprints — and this is the part of it a provider client is built from.
    """

    @property
    def model(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def provider_base_url(self) -> str: ...
    @property
    def temperature(self) -> float: ...
    def check_served_by(self, served: str | None) -> bool: ...


async def connect_model(
    config: ModelChoice, *, api_key: str, layer: str = "openai", key_header: str | None = None
) -> tuple[LLMClient, bool]:
    """The `real` client for a resolved configuration, with its provider checked.

    The one place a composition root gets a provider client from, so the check
    cannot be skipped by a script that builds its own. Returns the client and
    whether the declared provider was verified; a declaration the endpoint
    contradicts raises `ProviderMismatch` before any turn runs (T-018).
    `layer="pydantic-ai"`: the Azure stack's model layer, imported only when
    chosen (T-099); `key_header` is where an APIM gateway reads the key.
    """
    if layer == "pydantic-ai":
        from agent_harness.llm.pydantic_ai import connect

        return await connect(config, api_key=api_key, key_header=key_header)
    client = GroqClient(
        api_key=api_key,
        base_url=config.provider_base_url,
        model=config.model,
        provider=config.provider,
        temperature=config.temperature,
    )
    return client, config.check_served_by(await client.served_provider())


def _from_wire(raw: object) -> ModelResponse:
    """The typed boundary — AHC-0001. One parse, one place, two outcomes.

    Everything here is defensive on purpose. A provider response is the least
    trustworthy input the system takes: it is generated, it is truncated by
    token limits, and its shape is the vendor's to change. Every failure below
    was reachable before this function had a `try` at all — truncated tool
    arguments raised `JSONDecodeError` straight out of the client, past the
    loop's `except ModelUnavailable`, and out of the agent.
    """
    try:
        # `object`, read through getattr, on purpose: the response is untrusted
        # input, and typing it as the SDK's class would claim a shape the
        # validation below exists to check.
        choice = getattr(raw, "choices")[0]  # noqa: B009 — untrusted shape
    except (AttributeError, IndexError, TypeError) as exc:
        raise ModelMalformed(f"no choice in the response: {exc}") from exc

    message = choice.message
    calls: list[ToolCall] = []
    for call in message.tool_calls or []:
        # Arguments are parsed, never string-matched: escaping differs by
        # provider and by model generation. A token limit that truncates the
        # JSON mid-object is the common case, not an exotic one.
        text = getattr(call.function, "arguments", "") or "{}"
        try:
            arguments = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelMalformed(
                f"tool call {call.function.name!r} has unreadable arguments: {exc}", raw=text
            ) from exc
        # A JSON scalar is valid JSON and not a call. Without this the error
        # moves one layer up and arrives as a failure about the tool instead of
        # about the model, which sends whoever debugs it to the wrong file.
        if not isinstance(arguments, dict):
            raise ModelMalformed(
                f"tool call {call.function.name!r} arguments are {type(arguments).__name__}, "
                "not an object",
                raw=text,
            )
        calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
    usage = getattr(raw, "usage")  # noqa: B009 — untrusted shape
    spent = getattr(usage, "completion_tokens", 0) or 0
    if not (message.content or "").strip() and not calls:
        # Nothing usable came back, and it was paid for (F-031). A reasoning
        # model spends the output budget on reasoning and returns empty content
        # when the budget runs out, which reaches the customer as an empty reply
        # and the run as a success. It is a *malformed* completion, not an
        # answer: the same class as unparseable tool arguments, and it takes the
        # same declared path.
        raise ModelMalformed(
            f"the model returned neither text nor a tool call after {spent} output tokens"
            + (
                " — the output budget was spent before an answer began"
                if choice.finish_reason == "length"
                else ""
            ),
            raw=str(message.content or ""),
        )
    return ModelResponse(
        text=message.content or "",
        tool_calls=tuple(calls),
        usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
        model=getattr(raw, "model", "") or "",
        stop_reason=choice.finish_reason or "",
    )


class ScriptedClient:
    """`mock` resolution. Deterministic, free, no network.

    Not a test double bolted on — this is the adapter a sealed run uses, and it
    is what makes a whole eval suite affordable to run on every prompt edit.
    Exhausting the script is an error rather than a silent empty reply, because
    a scenario that runs longer than its script is a scenario that did not test
    what it claimed to.
    """

    def __init__(self, responses: Iterable[ModelResponse]) -> None:
        self._queue: deque[ModelResponse] = deque(responses)
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: "scripted",
                tel.GEN_AI_SYSTEM: "scripted",  # deprecated; emitted during migration
                tel.GEN_AI_OPERATION: "chat",
            },
        ) as span:
            if not self._queue:
                raise ModelUnavailable(f"scripted client exhausted after {len(self.calls)} calls")
            response = self._queue.popleft()
            tel.set_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response

    @property
    def exhausted(self) -> bool:
        return not self._queue


class UnavailableClient:
    """A provider that is always down. Turns AAC-0009 — graceful degradation on
    provider failure — into a case anyone can run."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelUnavailable("provider is unavailable (injected)")


__all__ = [
    "ModelChoice",
    "PROVIDER_HOSTS",
    "GroqClient",
    "ScriptedClient",
    "UnavailableClient",
    "connect_model",
    "failure_from",
    "provider_from_host",
    "provider_from_model_info",
    "retry_after_of",
]
