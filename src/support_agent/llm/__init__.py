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
    ModelRequest,
    ModelResponse,
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


class GroqClient:
    """`real` resolution. Groq over its OpenAI-compatible endpoint.

    The SDK's own retry handles transient failures; exhaustion becomes
    `ModelUnavailable`, which callers translate into a declared degradation path
    (AAC-0009). A provider error never reaches a customer as a stack trace.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_retries: int = 2,
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
                tel.GEN_AI_SYSTEM: "groq",
                tel.GEN_AI_OPERATION: "chat",
                tel.GEN_AI_REQUEST_MODEL: self._model,
            },
        ) as span:
            try:
                raw = await self._client.chat.completions.create(
                    model=self._model,
                    messages=_to_wire(request.messages),  # type: ignore[arg-type]
                    tools=list(request.tools) or None,  # type: ignore[arg-type]
                    max_tokens=request.max_tokens,
                    temperature=request.temperature or self._temperature,
                )
            except (RateLimitError, APIConnectionError, APIError) as exc:
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
    choice = raw.choices[0]  # type: ignore[attr-defined]
    message = choice.message
    calls: list[ToolCall] = []
    for call in message.tool_calls or []:
        calls.append(
            ToolCall(
                id=call.id,
                name=call.function.name,
                # Arguments are parsed, never string-matched: escaping differs
                # by provider and by model generation.
                arguments=json.loads(call.function.arguments or "{}"),
            )
        )
    usage = raw.usage  # type: ignore[attr-defined]
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
            **{tel.GEN_AI_SYSTEM: "scripted", tel.GEN_AI_OPERATION: "chat"},
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


__all__ = ["GroqClient", "ScriptedClient", "UnavailableClient"]
