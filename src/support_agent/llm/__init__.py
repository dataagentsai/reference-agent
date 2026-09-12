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
cassette, wrapping whichever of these it was recorded from.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable

from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from support_agent import telemetry as tel
from support_agent.contracts import (
    Message,
    ModelMalformed,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
    ToolCall,
    Usage,
)


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
        temperature: float = 0.0,
        max_retries: int = 0,
        timeout_s: float = 60.0,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout_s,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: "groq",
                tel.GEN_AI_SYSTEM: "groq",  # deprecated; emitted during migration
                tel.GEN_AI_OPERATION: "chat",
                tel.GEN_AI_REQUEST_MODEL: self._model,
            },
        ) as span:
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
                    temperature=request.temperature or self._temperature,
                )
            except RateLimitError as exc:
                raise ModelThrottled(str(exc), retry_after=retry_after_of(exc)) from exc
            except (APIConnectionError, APIError) as exc:
                raise ModelUnavailable(str(exc)) from exc

            response = _from_wire(raw)
            span.set_attribute(tel.GEN_AI_RESPONSE_MODEL, response.model)
            tel.set_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response


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


__all__ = ["retry_after_of", "GroqClient", "ScriptedClient", "UnavailableClient"]
