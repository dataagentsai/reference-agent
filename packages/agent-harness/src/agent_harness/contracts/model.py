"""The model call, as a shape rather than a vendor.

L2 · L13. Nothing here names a provider. The adapters satisfy these structurally,
which is what makes AHC-0022 — provider interaction is substitutable without
changing the system — true rather than merely claimed.

Note this is deliberately NOT an OpenAI-shaped schema. A `base_url` swap on an
OpenAI client reaches Groq, Cerebras, Together, OpenRouter and vLLM, but not
Anthropic, whose content blocks and cache control do not survive the shim. The
seam has to be ours.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.contracts.failures import AgentFailure, Fault


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, object]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    """Which tool a `tool` message answers.

    Redundant with `tool_call_id` in principle and required in practice: some
    providers render the transcript from the tool *name*, and a result whose
    name they cannot resolve is a 400 rather than a degraded answer.
    """
    tool_calls: tuple[ToolCall, ...] = ()
    """The calls an `assistant` turn made.

    Dropping these was the first defect the live provider found. A `tool` message
    references an id, and if the assistant turn that produced that id is not in
    the transcript there is nothing for it to reference. Scripted tests never
    caught it because they never serialised anything.
    """
    provenance: Literal["operator", "user", "tool", "retrieved"] = "operator"
    """Where this text came from.

    Tool output re-enters context through the same assembly path as any other
    untrusted material and is never appended as though the system authored it
    — AHC-0045. An instruction planted in an order note is the injection path
    that survives every input filter, because the hostile text never passed
    through the input.

    A summary inherits the provenance of its source: a summary of untrusted
    content is untrusted content.
    """


class Usage(BaseModel):
    """What the call cost. Attributed to a unit of work at L13, never to a call.

    AAC-0008 is cost per *successful task*, not cost per call — an agent that
    fails cheaply four times and succeeds on the fifth has not been cheap.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[Message, ...]
    tools: tuple[dict[str, object], ...] = ()
    max_tokens: int = 4096
    temperature: float | None = None
    """None takes the client's configured temperature, which is zero by default:
    determinism is the floor, variance is opted into. A number is this call's
    own. It was `0.0` meaning *unset*, so asking for zero under a non-zero
    configuration was read as not asking (F-064)."""


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return len(self.tool_calls) > 0


class ModelUnavailable(AgentFailure):
    """The provider could not be reached, after the adapter's own retries.
    Callers translate this into a declared degradation path (AAC-0009); it never
    reaches the customer as a stack trace. A refusal and an exhausted budget are
    subclasses with kinds of their own.
    """

    fault = Fault.UNREACHABLE


class ModelThrottled(ModelUnavailable):
    """The provider is rate-limiting us — a condition distinct from failure
    (AHC-0021). It says when to come back, it is not evidence the provider is
    down, and so it never counts against a circuit breaker."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ModelRefused(ModelUnavailable):
    """The provider, or a gateway in front of it, was reached and said no: a key
    it does not accept, a model this caller may not use, a request it will not
    take. Trying again gets the same answer, so it is never retried and never
    counts against a breaker; a refusal is not evidence anything is down.

    A subclass of `ModelUnavailable` so every declared degradation path that
    handles an unavailable model handles this one without a new branch. Its kind
    is what differs, and `ResilientLLM` reads the type. Until T-029 every 4xx
    other than 429 was `ModelUnavailable`: a revoked key was retried three times
    and opened the breaker.
    """

    fault = Fault.REFUSED


class ModelBudgetExhausted(ModelUnavailable):
    """The gateway's budget for this caller is spent (T-029).

    Its own type because the wire does not tell it apart: LiteLLM answers an
    exhausted budget with HTTP 429, the status of a rate limit, and without this
    the agent waited and retried against a bound that resets in weeks. A rate
    limit says *come back soon*; this says *not until somebody raises the bound*.
    """

    fault = Fault.EXHAUSTED


class ModelMalformed(AgentFailure):
    """The provider answered and the answer could not be read.

    A different condition from `ModelUnavailable` and deliberately its own type.
    Unavailable means nothing came back and retrying may work; malformed means
    something came back and *this model, on this prompt, produced something
    unusable* — retrying the identical request is the least likely thing to help.

    AHC-0001 requires that a parse failure be **a declared return shape callers
    must handle, not an exception raised from wherever the parse happened to
    fail**. Truncated tool-call arguments are the common case — a token limit
    cuts the JSON mid-object — and without this they surfaced as a
    `JSONDecodeError` escaping the client, past the loop's handler, out of the
    agent.

    `raw` is carried for the operator and never for the customer. The capability
    flags the tension outright: discarding the raw response makes the failure
    unexplainable later, and retaining it puts model output that may echo
    sensitive input into storage. It is held in memory on the error, redacted by
    `telemetry` before it is ever recorded, and not persisted.
    """

    def __init__(self, reason: str, *, raw: str = "") -> None:
        self.reason = reason
        self.raw = raw
        super().__init__(reason)

    fault = Fault.MALFORMED
