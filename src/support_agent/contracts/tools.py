"""The tool surface, as a shape.

L3 · L16 · L10. Two details here are load-bearing and both come from the MCP
specification rather than from taste.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from support_agent.contracts.domain import SideEffectClass


class ToolSpec(BaseModel):
    """A tool as advertised by `tools/list`.

    `output_schema` is optional in the MCP specification and mandatory here. Where
    a tool declares one, servers MUST return structured results conforming to it —
    which is what turns world-state assertions from parsing into comparison. A
    tool without one cannot be asserted against cheaply, so it is rejected at
    registration.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    side_effect: SideEffectClass
    required_scope: str | None = None


class ToolResult(BaseModel):
    """What came back.

    `structured` is authoritative; `text` exists only because MCP asks a server
    returning structured content to also serialise it into a text block for
    backward compatibility. The same data crosses the wire twice in two
    encodings — read `structured`, and assert that `text` agrees rather than
    reading from it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    structured: object = None
    text: str = ""
    is_error: bool = False
    error_channel: Literal["none", "execution", "protocol"] = "none"
    """MCP reports failure through two distinct channels, and an agent that
    handles one and not the other looks healthy until production.

    `execution` — a normal result with `isError: true`: API failure, validation,
    business logic. The model is expected to self-correct from these.
    `protocol` — a JSON-RPC error: unknown tool, malformed request. The model is
    much less likely to recover, and these usually indicate our bug, not its.
    """

    duration_ms: int = 0
    truncated: bool = False
    """Set when the result was bounded before entering context — AAC-0105."""


class ToolRegistry(BaseModel):
    """The action surface available to one identity.

    `tools/list` may vary by the authorization presented on the request — it
    must not vary per connection, but it may return only the tools the caller's
    scopes permit. So the surface itself is authorization-scoped, which is what
    makes the confused-deputy case testable at the protocol level rather than
    only inside a tool body.
    """

    model_config = ConfigDict(frozen=True)

    tools: tuple[ToolSpec, ...] = ()

    def get(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def irreversible(self) -> tuple[ToolSpec, ...]:
        return tuple(t for t in self.tools if t.side_effect is SideEffectClass.IRREVERSIBLE)


class ToolUnavailable(Exception):
    """The tool server could not be reached. Distinct from a tool that ran and
    failed, which is a `ToolResult` with `is_error` set — that one the model
    can act on."""


class UnknownTool(Exception):
    """The model asked for a tool that is not in the registry for this identity.

    Recoverable: the loop reports it back so the model can choose again
    (AAC-0051, tool selection accuracy), rather than crashing the run.
    """

    def __init__(self, name: str, available: tuple[str, ...] = ()) -> None:
        self.name = name
        self.available = available
        super().__init__(f"unknown tool {name!r}")


class MissingIdempotencyKey(Exception):
    """Raised when a non-READ tool is invoked without a key.

    Should be unreachable: `ToolClient.call` takes the key as a required
    argument, so the type system enforces AHC-0074 rather than a convention
    doing it. This exists to make the failure loud if that ever stops being
    true.
    """


class Approval(BaseModel):
    """A pending human decision. Lives in agent-owned state, never in the
    simulated world — if the oracle can be faked, it is not an oracle."""

    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, object] = Field(default_factory=dict)
    reason: str
    decided: bool = False
    granted: bool = False
    decided_by: str | None = None
