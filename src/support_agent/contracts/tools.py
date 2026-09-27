"""The tool surface, as a shape.

L3 · L16 · L10. Two details here are load-bearing and both come from the MCP
specification rather than from taste.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from support_agent.contracts.domain import SideEffectClass
from support_agent.contracts.failures import AgentFailure, Fault


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
    entity: str = ""
    """Which kind of row this tool is about, as the far end declares it.

    Optional, and empty when a server does not say — which is why every use of
    it falls back rather than failing. It exists for AHC-0107: re-reading a row
    before an irreversible action means knowing *which* reader reads that row,
    and on a surface with one entity the question does not arise. On a surface
    with `get_order` and `get_shipment` it is the whole question, and an id
    alone cannot answer it (T-061).
    """


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
    """Set when the result was bounded before entering context — AAC-0105. The
    bounded rendering is then `text`, and `structured` stays whole for the checks
    that read it (grounding, assertions) but never enters context."""

    def for_context(self) -> str:
        """What of this result enters the model's context — the one rendering the
        bound measures and the context boundary sends.

        The structured content where present: the text block is its
        backward-compatible duplicate, and sending both would put the same data
        through the fence twice. F-023 was the bound measuring one of these while
        the context sent the other.
        """
        if self.truncated or self.structured is None:
            return self.text
        return str(self.structured)


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

    def reader_for(self, entity: str = "") -> ToolSpec | None:
        """A tool that can look *this kind of row* up again without changing it.

        AHC-0107. The order within a match is the specification's own, because a
        surface offering two ways to read the same row would make this a choice,
        and a choice made here would be this module inventing policy.

        **What `entity` buys.** Without it this returned the first read on the
        surface whatever it read, so a shipment id was re-read with `get_order`
        the moment a second entity existed (T-061). With it the reader is the
        one that reads this entity, and when the entity is known and no reader
        claims it the answer is `None` — *refusing to guess*, because reading
        the wrong row and calling the belief fresh is worse than saying the
        belief cannot be checked.

        **Why it still falls back.** A server that declares no entity leaves
        every spec's `entity` empty, and on such a surface the first read is the
        only answer available and was always the answer. So an undeclared
        surface behaves exactly as it did.
        """
        reads = [t for t in self.tools if t.side_effect is SideEffectClass.READ]
        if entity:
            matched = [t for t in reads if t.entity == entity]
            if matched:
                return matched[0]
            if any(t.entity for t in reads):
                # Somebody on this surface says what they read, and none of them
                # says this. That is an answer, not an absence.
                return None
        return reads[0] if reads else None


class ToolUnavailable(AgentFailure):
    """The tool server could not be reached. Distinct from a tool that ran and
    failed, which is a `ToolResult` with `is_error` set — that one the model
    can act on."""

    fault = Fault.UNREACHABLE


class UnknownTool(AgentFailure):
    """The model asked for a tool that is not in the registry for this identity.

    Recoverable: the loop reports it back so the model can choose again
    (AAC-0051, tool selection accuracy), rather than crashing the run.
    """

    def __init__(self, name: str, available: tuple[str, ...] = ()) -> None:
        self.name = name
        self.available = available
        super().__init__(f"unknown tool {name!r}")

    fault = Fault.MISCONFIGURED


class MissingIdempotencyKey(AgentFailure):
    """Raised when a non-READ tool is invoked without a key.

    Should be unreachable: `ToolClient.call` takes the key as a required
    argument, so the type system enforces AHC-0074 rather than a convention
    doing it. This exists to make the failure loud if that ever stops being
    true.
    """

    fault = Fault.MISCONFIGURED


class ApprovalRequested(Exception):  # noqa: N818 — control flow, not a failure
    """A harness-local tool handed a decision to a person, and the loop must stop.

    The one thing a local tool can do that the loop cannot express as a
    `ToolResult`: continuing would let the model narrate an effect nobody has
    authorised. Generic on purpose — the loop catches this and never knows which
    action it was, so a second approvable action adds a raise, not a loop edit.
    `reply` is what the customer is told while they wait.
    """

    def __init__(self, approval: Approval, reply: str) -> None:
        self.approval = approval
        self.reply = reply
        super().__init__(approval.id)


class ApprovalState(StrEnum):
    """Where an approval is in its workflow (T-028).

    The workflow owns every move between these, so no caller can write one: a
    person's decision reaches it as a request the workflow may refuse, and the
    action is carried out by the workflow itself, not by the agent that asked.
    """

    ASSESSING = "assessing"
    """Reading what the action would do, to decide whether a person is needed."""
    WAITING = "waiting"
    """With a person, until they decide or the approval expires."""
    CARRYING_OUT = "carrying_out"
    DONE = "done"
    """Granted and carried out. `result` is what the far end said."""
    FAILED = "failed"
    """Could not be assessed, or was granted and the far end refused it."""
    STALE = "stale"
    """Granted, and the facts it was granted against had changed by the time it
    ran (F-054). Told apart from `FAILED` because the two ask different things
    of an operator: a failure is the far end refusing, and this is nobody's
    fault — the world moved during a human wait, and the decision has to be
    made again against today's facts."""
    REFUSED = "refused"
    """A person said no."""
    EXPIRED = "expired"
    """Nobody decided in time. A grant that arrives later is refused."""


class Approval(BaseModel):
    """A human decision about one action, and what became of it.

    Lives in the approval workflow, never in the simulated world — if the
    oracle can be faked, it is not an oracle."""

    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, object] = Field(default_factory=dict)
    reason: str
    customer_id: str
    conversation_id: str = ""
    """Which conversation asked for it. Empty where a caller had none — a test,
    a script — and what a reminder is addressed to when one waits too long
    (T-059): the desk finds the conversation by it, and the channel finds the
    inbox, because a conversation id carries the channel's own ids."""
    decided_against: dict[str, str] = Field(default_factory=dict)
    """The facts this decision was made about, as they read when it was assessed.

    F-054. Every other field here says *what was decided*; none of them said
    what it was decided **against**, and an approval is the one place in the
    system where minutes or hours pass between reading a row and acting on it.
    Where the amount is a field on a row rather than an argument on the call —
    `amount_from: order.total` — both sides read it separately, an hour apart,
    and the far end's check compares argument values it never sees move. So a
    person approves ₹25,000, the row changes underneath, and ₹41,000 leaves.

    Values are strings so that what is compared is what was recorded, whatever
    the far end's JSON did with the types between two reads. Which fields belong
    here is the assessment's to say: they are the ones its decision depended on,
    and a field nobody judged has no business invalidating a grant.
    """

    idempotency_key: str
    """The key minted when the action was *requested*, carried across the wait.

    This is what links L14 to L10. An approval granted an hour later executes
    under the original key, so a resume that happens twice — a retry, a duplicate
    click, a second process — still produces one effect. A key minted at
    execution time would defeat the whole ledger.
    """
    created_at: int
    expires_at: int
    decided: bool = False
    granted: bool = False
    decided_by: str | None = None
    state: ApprovalState = ApprovalState.WAITING
    result: str | None = None
    """What carrying it out produced, or why it could not be assessed."""
    supersedes: str | None = None
    """The stale grant this approval asks again, when it is one (P-APPROVAL-STALE)."""
    superseded_by: str | None = None
    """The fresh approval a stale grant was asked again as. Set once, with
    `STALE`: a grant whose facts moved is not retried — a person decides the
    order as it now is, and the two records name each other so the desk and
    the conversation can both follow the request from one to the next."""

    @property
    def execution(self) -> Execution | None:
        """What happened when a grant was carried out — the AOAS `execution`.

        Derived from `state` rather than stored beside it, so the two cannot
        disagree. `None` until a grant has been acted on, and for an approval
        nobody granted: *refused* here is the far end refusing a grant, not a
        person saying no, which is `REFUSED` and never reaches execution.
        """
        return EXECUTION.get(self.state) if self.granted else None


Execution = Literal["done", "refused", "stale", "grant_expired"]
EXECUTION: dict[ApprovalState, Execution] = {
    ApprovalState.DONE: "done",
    ApprovalState.FAILED: "refused",
    ApprovalState.STALE: "stale",
    ApprovalState.EXPIRED: "grant_expired",
}
"""The AOAS `approval.execution` values, read off the states that already hold
them. A stale grant is `stale` and never `refused`: nothing was attempted, so
there is nothing for an operator to find broken (AHC-0057 `stale_grant`)."""


@dataclass(frozen=True)
class LocalTool:
    """A tool the harness answers itself, never dispatched over MCP.

    Needed because some actions belong to the agent rather than to any external
    system. Raising an approval is the example: the decision lives in
    agent-owned state, so no tool on the business server could create one, and
    pretending otherwise would put the oracle inside the world being simulated.

    Advertised to the model exactly like any other tool, so the model does not
    need to know the difference — but dispatched locally, so it never crosses
    the MCP boundary and AgentTwin never has to project it.
    """

    spec: ToolSpec
    handler: Callable[[dict[str, object]], Awaitable[ToolResult]]


class Unbindable(AgentFailure):
    """These arguments cannot be fitted to that tool's declared schema."""

    fault = Fault.REFUSED


def bind_arguments(spec: ToolSpec, args: dict[str, object]) -> dict[str, object]:
    """Map arguments onto whatever the tool actually declares.

    The caller knows it found an order id; it does not know what this world calls
    that field. One required string property means one place to put it.

    **Extended for the resume path (F-013).** The router hands over exactly one
    argument, which made the single-required/single-argument rule sufficient. An
    approval does not: it stores `{order_id, amount}` from the harness-local
    request tool, and the projected `issue_refund` declares only the entity's
    key. So two more steps, in order of confidence:

    1. **Keep what the tool declares.** An argument the schema does not mention
       is dropped rather than passed — `additionalProperties: false` would reject
       the whole call for it.
    2. **Fill a missing required slot by name.** `order_id` for a required `id`
       is the `<entity>_<key>` convention, and it is checked rather than assumed:
       the spare must equal the required name, or end with `_id`-style suffix, or
       carry it as a prefix. Exactly one candidate or nothing.

    Ambiguity raises. On the path where money moves, guessing between two spare
    values is worse than stopping — and stopping with a message beats the
    `jsonschema` exception that was surfacing through three nested task groups.
    """
    declared = spec.input_schema.get("properties")
    properties: dict[str, object] = declared if isinstance(declared, dict) else {}
    listed = spec.input_schema.get("required")
    required = (
        [n for n in listed if isinstance(n, str) and n in properties]
        if isinstance(listed, list)
        else []
    )
    if len(required) == 1 and len(args) == 1:
        return {required[0]: next(iter(args.values()))}

    kept = {n: v for n, v in args.items() if n in properties}
    spare = {n: v for n, v in args.items() if n not in properties}

    for name in [n for n in required if n not in kept]:
        candidates = [n for n in spare if _reads_as(n, name)]
        if len(candidates) != 1:
            raise Unbindable(
                f"{spec.name} requires {name!r} and the stored arguments "
                f"{sorted(args)} offer {candidates or 'nothing'} for it"
            )
        kept[name] = spare.pop(candidates[0])

    return kept


def _reads_as(offered: str, required: str) -> bool:
    return (
        offered == required
        or offered.endswith(f"_{required}")
        or offered.startswith(f"{required}_")
    )
