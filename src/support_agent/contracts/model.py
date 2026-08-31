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


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
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


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, object]


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
    temperature: float = 0.0
    """Zero by default. Determinism is the floor; variance is opted into."""


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


class ModelUnavailable(Exception):
    """The provider could not be reached, or refused, after the adapter's own
    retries. Callers translate this into a declared degradation path (AAC-0009);
    it never reaches the customer as a stack trace.
    """
