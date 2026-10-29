"""`real` resolution on Claude, through the Anthropic SDK's Messages API.

The first adapter that is not OpenAI-shaped, and so the first real test of
AHC-0022 (provider interaction is substitutable without changing the system):
Claude's content blocks, thinking and cache control do not survive an
OpenAI-compatible shim, so the translation lives here, at the edge, and nowhere
else (clinical-data-review-agent F-3).

What this adapter guarantees, each for a reason:

- **No retries of its own** (`max_retries=0`). The Anthropic SDK retries twice
  by default, invisibly; AHC-0024 gives retries to `resilience.ResilientLLM`,
  which wraps this client, so one unit of work cannot quietly cost three calls.
- **Thinking blocks go back unchanged.** Every response's content is kept,
  verbatim, in `ModelResponse.provider_blocks`; the loop stores it on the
  assistant `Message`, and this adapter sends it back as that turn's content.
  Current Claude models require that in a tool loop (F-1). Run them with
  `Budgets.max_context_chars=None`, so the transcript is append-only (F-2).
- **No sampling parameters.** `ModelRequest.temperature` is ignored: current
  Claude models reject any temperature, and determinism comes from recorded
  responses and replay, not sampling (F-4). Effort is this client's setting.
- **Refusals are typed.** `stop_reason == "refusal"` raises `ModelRefused`
  with the category, so the run ends as refused and is never retried; with
  `fallbacks` set, the API first retries on Anthropic's substitute model, and
  `ModelResponse.model` names the model that actually answered (AAC-0100).
- **The stable prefix is cached.** Tools and the system prompt are marked
  for prompt caching; cache reads arrive as `Usage.cached_input_tokens`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from agent_harness import telemetry as tel
from agent_harness.contracts import (
    Message,
    ModelMalformed,
    ModelRefused,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
    ToolCall,
    Usage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}
"""Anthropic's stop reasons in the loop's words; `length` is what the loop treats
as clipped. `pause_turn` (server tools) is not used by this adapter."""


def tools_to_wire(tools: Iterable[dict[str, object]]) -> list[dict[str, Any]]:
    """The loop's tool definitions (OpenAI-shaped, `context.model_tools`) as
    Anthropic tools. Tools render before the system prompt, so the breakpoint on
    the last system block caches both as one stable prefix."""
    wire: list[dict[str, Any]] = []
    for tool in tools:
        fn: Any = tool.get("function", tool)
        wire.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return wire


def messages_to_wire(
    messages: Sequence[Message],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the transcript into Anthropic's top-level system blocks and messages.

    - Leading `system` messages become the system prompt; a later one becomes a
      mid-conversation `system` message, which current models accept.
    - An assistant turn with `provider_blocks` is sent back as exactly those
      blocks; without them, as text plus `tool_use` blocks.
    - Consecutive `tool` messages become **one** user message of `tool_result`
      blocks: parallel results split across messages teach the model to stop
      calling tools in parallel.
    """
    system: list[dict[str, Any]] = []
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system" and not wire:
            system.append({"type": "text", "text": m.content})
        elif m.role == "tool":
            _add_tool_result(wire, m)
        elif m.role == "assistant":
            wire.append({"role": "assistant", "content": _assistant_content(m)})
        else:  # a user turn, or a mid-conversation system message
            wire.append({"role": m.role, "content": m.content})
    if system:
        system[-1]["cache_control"] = {"type": "ephemeral"}
    return system, wire


def _assistant_content(m: Message) -> list[dict[str, Any]]:
    """Verbatim when the provider's blocks were kept; rebuilt otherwise."""
    if m.provider_blocks:
        return [dict(b) for b in m.provider_blocks]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
    blocks += [
        {"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.arguments)}
        for c in m.tool_calls
    ]
    return blocks or [{"type": "text", "text": ""}]


def _add_tool_result(wire: list[dict[str, Any]], m: Message) -> None:
    """Join the previous message when it already holds only tool results."""
    result = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
    previous = wire[-1] if wire else None
    if (
        previous is not None
        and previous["role"] == "user"
        and isinstance(previous["content"], list)
        and all(b.get("type") == "tool_result" for b in previous["content"])
    ):
        previous["content"].append(result)
    else:
        wire.append({"role": "user", "content": [result]})


def response_from_wire(raw: Any) -> ModelResponse:
    """The typed boundary (AHC-0001): one parse, read by block `type`, never by position."""
    try:
        blocks = list(raw.content)
        stop = raw.stop_reason
        usage = raw.usage
    except AttributeError as exc:
        raise ModelMalformed(f"not a Messages API response: {exc}") from exc
    if stop == "refusal":
        details = getattr(raw, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        raise ModelRefused(f"the model declined (category: {category or 'unspecified'})")

    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "text":
            texts.append(block.text)
        elif kind == "tool_use":
            if not isinstance(block.input, dict):
                raise ModelMalformed(
                    f"tool call {block.name!r} has non-object input", raw=str(block.input)
                )
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    return ModelResponse(
        text="".join(texts),
        tool_calls=tuple(calls),
        usage=Usage(
            # Uncached input, with cache writes priced as input (see cost.PRICES).
            input_tokens=(usage.input_tokens or 0) + cache_write,
            output_tokens=usage.output_tokens or 0,
            cached_input_tokens=cache_read,
        ),
        model=getattr(raw, "model", "") or "",
        stop_reason=STOP.get(stop, stop or ""),
        provider_blocks=tuple(_verbatim(b) for b in blocks),
    )


def _verbatim(block: Any) -> dict[str, object]:
    """A content block as the API sent it: only the fields it set, nothing added."""
    to_dict = getattr(block, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return dict(block.model_dump(exclude_unset=True))


def failure_from(error: Exception) -> ModelUnavailable:
    """What an Anthropic SDK error means for the caller, by kind (as `llm.failure_from`)."""
    import anthropic

    if isinstance(error, anthropic.RateLimitError):
        from agent_harness.llm import retry_after_of

        return ModelThrottled(str(error), retry_after=retry_after_of(error))
    if isinstance(
        error,
        anthropic.AuthenticationError
        | anthropic.PermissionDeniedError
        | anthropic.NotFoundError
        | anthropic.BadRequestError
        | anthropic.UnprocessableEntityError,
    ):
        return ModelRefused(str(error))
    return ModelUnavailable(str(error))


class AnthropicClient:
    """Claude through the Anthropic SDK. An `LLMClient`."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = "medium",
        fallbacks: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        """`base_url` is the swap point: Anthropic directly, or a Messages-API
        endpoint such as Databricks Model Serving, with no other change.
        `client` is a test's stand-in for the SDK client."""
        self._model = model
        self._effort = effort
        self._fallbacks = fallbacks
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout_s
            )
        self._client = client

    def request_body(self, request: ModelRequest) -> dict[str, Any]:
        system, messages = messages_to_wire(request.messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        tools = tools_to_wire(request.tools)
        if tools:
            body["tools"] = tools
        if self._effort:
            body["output_config"] = {"effort": self._effort}
        return body

    async def complete(self, request: ModelRequest) -> ModelResponse:
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: "anthropic",
                tel.GEN_AI_SYSTEM: "anthropic",
                tel.GEN_AI_OPERATION: "chat",
                tel.GEN_AI_REQUEST_MODEL: self._model,
            },
        ) as span:
            standard = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "anthropic",
                "gen_ai.request.model": self._model,
            }
            body = self.request_body(request)
            started = time.monotonic()
            try:
                if self._fallbacks is not None:
                    raw = await self._client.beta.messages.create(
                        **body, betas=[FALLBACK_BETA], fallbacks=self._fallbacks
                    )
                else:
                    raw = await self._client.messages.create(**body)
            except Exception as exc:
                failure = _typed(exc)
                tel.counters.operation_duration.record(
                    time.monotonic() - started, standard | {"error.type": type(failure).__name__}
                )
                raise failure from exc

            response = response_from_wire(raw)
            standard["gen_ai.response.model"] = response.model
            tel.counters.operation_duration.record(time.monotonic() - started, standard)
            for kind, count in (
                ("input", response.usage.input_tokens + response.usage.cached_input_tokens),
                ("output", response.usage.output_tokens),
            ):
                tel.counters.token_usage.record(
                    count,
                    standard | {"gen_ai.token.type": kind, "config": tel.counters.configuration()},
                )
            span.set_attribute(tel.GEN_AI_RESPONSE_MODEL, response.model)
            span.set_attribute(tel.GEN_AI_CACHE_READ_TOKENS, response.usage.cached_input_tokens)
            tel.set_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response


def _typed(exc: Exception) -> Exception:
    """SDK errors become the harness's typed failures; anything else passes through."""
    try:
        import anthropic
    except ImportError:  # a test's stand-in client without the SDK installed
        return exc
    if isinstance(exc, anthropic.APIError):
        return failure_from(exc)
    return exc


__all__ = [
    "AnthropicClient",
    "failure_from",
    "messages_to_wire",
    "response_from_wire",
    "tools_to_wire",
]
