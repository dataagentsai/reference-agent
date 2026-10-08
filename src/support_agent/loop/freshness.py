"""AHC-0107 — what the run believes about a row, and when it last checked.

The world has other writers. `concurrent_writers: [warehouse, carrier]` is in
the order's own declaration, which is the specification saying out loud that a
read of `status` is a claim about the past. The gap between that read and the
action taken on it is exactly as long as the model took to think, and a
cancellation planned against *pending* arrives at a row that shipped while the
customer was answering a question.

**Why this is not the far system's job.** The order system does enforce its own
preconditions, and it does refuse the stale write — that is `stale-read-then-
refused`, and it passes. What it cannot do is stop the agent having already
composed a reply from the stale value. Both sentences trace to the same lookup:
the customer is told the order has not shipped by an agent that is
simultaneously being refused permission to cancel it. AAC-0113 asks for both
halves for that reason, and only the caller can hold the second.

**What it does, and deliberately does not do.** It answers one question — *is
what this run believes about this row still inside the window the specification
gave it* — and the loop acts on the answer by re-reading rather than by
deciding. Nothing here knows what a refund is, and nothing here decides whether
an action should happen: that is authority, and it is elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from opentelemetry.trace import Span

from agent_harness import telemetry as tel
from agent_harness.context import tool_message
from support_agent.contracts import (
    IdempotencyKey,
    Identity,
    Message,
    RunId,
    SideEffectClass,
    ToolCall,
    ToolClient,
    ToolRegistry,
    ToolResult,
)
from support_agent.loop.ends import Trace


@dataclass
class Freshness:
    """When each row this run has read was last read, and whether that will do.

    Keyed by the row's kind and its identifier — `cache_key` builds it. The kind
    comes from what the *server* declared about its own tools, never from this
    module knowing the domain: the loop still sees only that `get_order` returns
    a row and `cancel_order` takes an id, and still could not say what an order
    is. What it can now say is that the two are about the same kind of thing.

    It was keyed by the identifier alone until T-061, which held while every
    operation on the surface was about one entity and stopped holding the moment
    a second arrived: `order AB-1` and `item AB-1` are different rows and shared
    one entry.
    """

    window_s: int | None = None
    """How long a read stays usable. `None` disables the whole mechanism, which
    is the honest default for a binding that declared no window: a system with
    one writer has nothing to go stale."""

    read_at: dict[str, int] = field(default_factory=dict)
    value: dict[str, object] = field(default_factory=dict)
    """What the row held when it was last read. Kept because *when* is only half
    the question: a re-read that agrees with what was believed changes nothing
    about the action that was planned, and stopping to ask the model about it
    would be stopping for no reason."""

    def saw(self, key: str, now: int, value: object = None) -> None:
        """Record that this row was read at this moment, and what it said."""
        if key:
            self.read_at[key] = now
            if value is not None:
                self.value[key] = value

    def remember(
        self, registry: ToolRegistry, call: ToolCall, now: int, value: object = None
    ) -> None:
        """What a read told this run about the row it was about.

        Here rather than in the loop, because composing the key means knowing a
        belief is keyed by kind as well as row, and that is this module's
        business. The loop asks only "was that a read", which it can answer.
        """
        self.saw(cache_key(entity_of(registry, call.name), key_of(call.arguments)), now, value)

    def stale(self, key: str, now: int) -> bool:
        """Whether acting on this row would be acting on an old belief.

        A row this run has never read is **not** stale: it has no belief to be
        old, and the far system's preconditions are what govern an action taken
        blind. Re-reading it here would be inventing a lookup the run never
        needed, and would make every first action cost two calls.
        """
        if self.window_s is None or not key:
            return False
        seen = self.read_at.get(key)
        return seen is not None and now - seen > self.window_s


def key_of(arguments: dict[str, object]) -> str:
    """The row an argument list is about.

    Both spellings: every tool the model sees takes the entity's key, `id`
    (AOAS, input names), and an approval's stored arguments keep the domain's
    `order_id`. A call naming no row is
    not about a row, and answers `""`.
    """
    for name in ("id", "order_id"):
        value = arguments.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


HELD = (
    "not run: what this run knew about that record was older than the "
    "specification allows an irreversible action to rely on, so it has been read "
    "again. The current values are in the result that follows."
)
"""What the model is told about the call that did not happen.

An error, because the call did not do what it asked — and a message that says
why and what to do next, because the alternative is a request of the model's own
vanishing without explanation, which teaches it nothing except that tools are
unreliable.
"""


@dataclass(frozen=True)
class Refreshed:
    """What a refresh produced, ready for the transcript.

    Messages and results rather than a decision, because the caller's only job
    is to put them where they go. Empty means nothing was stale, or everything
    stale turned out to be right — in both cases the step proceeds as planned.
    """

    messages: list[Message] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


async def refresh(
    planned: list[tuple[ToolCall, IdempotencyKey]],
    *,
    fresh: Freshness,
    registry: ToolRegistry,
    tools: ToolClient,
    identity: Identity,
    run_id: RunId,
    iteration: int,
    now: int,
    span: Span,
) -> Refreshed:
    """Read again every row a stale irreversible action would have acted on.

    Returns each read as the call that was made and the result it produced —
    both, because a result without its call is an orphan and the assembler
    refuses one (AHC-0103). The call is real: the harness made it. Putting it in
    the transcript is not a fiction about the model, it is the record saying
    what happened in the order it happened.

    Returns nothing when nothing was stale; an empty list means
    nothing was stale and the step proceeds as planned. The caller abandons the
    whole step when anything comes back, and that is deliberate: the calls in a
    step were planned together against one picture of the world, and running the
    rest of them against a picture that has just been shown to be wrong is the
    same mistake in smaller pieces.
    """
    # Grouped by entity as well as row, because the reader that can look a row
    # up again is the one that reads *that kind of row* — and on a surface with
    # one entity every group is the same group, so this costs nothing (T-061).
    rows: set[tuple[str, str]] = {
        (entity_of(registry, call.name), key_of(call.arguments))
        for call, _ in planned
        if _is(registry, call.name, SideEffectClass.IRREVERSIBLE)
        and fresh.stale(cache_key(entity_of(registry, call.name), key_of(call.arguments)), now)
    }
    if not rows:
        return Refreshed()

    out: list[tuple[ToolCall, ToolResult]] = []
    with tel.span("agent.freshness.refresh", **{tel.FRESHNESS_ROWS: len(rows)}):
        for entity, row in sorted(rows):
            reader = registry.reader_for(entity)
            if reader is None:
                # Nothing on this surface reads this kind of row. Saying so is
                # better than proceeding as though the belief were fresh, better
                # than failing the run, and — since `reader_for` refuses to guess
                # — better than re-reading some other entity and believing it.
                # The far system's own preconditions still govern what lands.
                span.set_attribute(tel.FRESHNESS_UNCHECKABLE, True)
                continue
            # `step=-1` says this read belongs to no step the model planned. It
            # is the harness's own, and a key that pretended otherwise would put
            # a call the model never made into the model's own numbering.
            key = IdempotencyKey(run_id=run_id, step=-1, iteration=iteration)
            call = ToolCall(id=f"fresh-{row}", name=reader.name, arguments={"id": row})
            cached = cache_key(entity, row)
            before = fresh.value.get(cached)
            result = await tools.call(reader.name, {"id": row}, identity, key)
            if not result.is_error and not _is_about(result.structured, row):
                # The reader answered, and answered about something else. Caching
                # it would make a belief about the wrong row look fresh, which is
                # the failure this whole module exists to prevent, arrived at from
                # the other direction.
                span.set_attribute(tel.FRESHNESS_UNCHECKABLE, True)
                continue
            if not result.is_error:
                fresh.saw(cached, now, result.structured)
                if result.structured == before:
                    # The belief was old and it was also right. Nothing the run
                    # planned is wrong, so nothing is abandoned and the model is
                    # not asked about a change that did not happen — it would
                    # only be asked again on the next turn, and the turn after
                    # that, because a model slower than the window makes every
                    # belief stale by the time it acts. That livelock is what
                    # this branch exists to prevent, and a test found it.
                    continue
            out.append((call, result))
    return _transcript(planned, out)


async def resume(
    earlier: tuple[str, ...],
    *,
    fresh: Freshness,
    registry: ToolRegistry,
    tools: ToolClient,
    identity: Identity,
    run_id: RunId,
    now: int,
) -> Refreshed:
    """Read again, before the model is asked anything, every row an earlier turn
    read (AHC-0117).

    A read from an earlier turn has no stamp this run can trust — a minute ago
    or nine days — so where a window is declared at all, each such row is read
    again the way it was first read, and the result follows the history as what
    is now true. The model is never the reason a stale value is caught (F-085).
    A row whose reader is gone from the surface, or that is not a read, is
    skipped: nothing here invents a lookup the surface does not offer.
    """
    if fresh.window_s is None:
        return Refreshed()
    out = Refreshed()
    with tel.span("agent.freshness.resume", **{tel.FRESHNESS_ROWS: len(earlier)}):
        for n, entry in enumerate(earlier):
            tool, _, row = entry.partition(":")
            if not row or not reads(registry, tool):
                continue
            # Its own key per row, and `step=-2` so no resumed read shares one
            # with a refresh (`-1`) or a step the model planned: a far end that
            # answers a repeated key from its record would hand one row's answer
            # back for another.
            key = IdempotencyKey(run_id=run_id, step=-2, iteration=n)
            call = ToolCall(id=f"resumed-{row}", name=tool, arguments={"id": row})
            result = await tools.call(tool, {"id": row}, identity, key)
            if not result.is_error and _is_about(result.structured, row):
                fresh.remember(registry, call, now, result.structured)
            out.messages.append(Message(role="assistant", content="", tool_calls=(call,)))
            out.results.append(result)
            out.messages.append(tool_message(result, tool_call_id=call.id))
    return out


class Run(Protocol):
    """What a re-read needs of the run it happens in, and what it adds to."""

    fresh: Freshness
    registry: ToolRegistry
    tools: ToolClient
    identity: Identity
    run_id: RunId
    span: Span
    messages: list[Message]
    seen_results: list[ToolResult]
    trace: Trace

    @property
    def now(self) -> Callable[[], int]: ...


async def refreshed(planned: list[tuple[ToolCall, IdempotencyKey]], run: Run) -> bool:
    """`refresh`, put into the run's transcript; whether anything was re-read."""
    held = await refresh(
        planned,
        fresh=run.fresh,
        registry=run.registry,
        tools=run.tools,
        identity=run.identity,
        run_id=run.run_id,
        iteration=len(run.trace.tool_calls),
        now=run.now(),
        span=run.span,
    )
    run.messages.extend(held.messages)
    run.seen_results.extend(held.results)
    return bool(held.messages)


async def resumed(earlier: tuple[str, ...], run: Run) -> None:
    """`resume`, put into the run's transcript before its first ask (AHC-0117)."""
    if earlier:
        again = await resume(
            earlier,
            fresh=run.fresh,
            registry=run.registry,
            tools=run.tools,
            identity=run.identity,
            run_id=run.run_id,
            now=run.now(),
        )
        run.messages.extend(again.messages)
        run.seen_results.extend(again.results)


def _transcript(
    planned: list[tuple[ToolCall, IdempotencyKey]], read: list[tuple[ToolCall, ToolResult]]
) -> Refreshed:
    """The exchange a refresh adds, in the order it happened.

    Nothing stale and wrong means nothing to say: the step runs as planned.

    Otherwise two things have to appear, and leaving out either one breaks the
    transcript (AHC-0103), which is how both were found rather than shipped.
    The model's own calls were already announced and are not going to run, so
    each is answered with why — the alternative is a request of the model's own
    vanishing without explanation, which teaches it nothing except that tools
    are unreliable. And the harness's read is announced as the call it was,
    because a result whose call is missing is the same orphan in the other
    direction.
    """
    if not read:
        return Refreshed()

    out = Refreshed()
    for call, _ in planned:
        held = ToolResult(name=call.name, text=HELD, is_error=True, error_channel="execution")
        out.results.append(held)
        out.messages.append(tool_message(held, tool_call_id=call.id))
    for call, result in read:
        out.messages.append(Message(role="assistant", content="", tool_calls=(call,)))
        out.results.append(result)
        out.messages.append(tool_message(result, tool_call_id=call.id))
    return out


def _is(registry: ToolRegistry, tool: str, kind: SideEffectClass) -> bool:
    spec = registry.get(tool)
    return spec is not None and spec.side_effect is kind


def entity_of(registry: ToolRegistry, tool: str) -> str:
    """What kind of row this tool is about, or `""` when the server does not say."""
    spec = registry.get(tool)
    return spec.entity if spec is not None else ""


def cache_key(entity: str, row: str) -> str:
    """How a row is remembered: by its kind as well as its id.

    An id is unique within an entity and nothing promises it is unique across
    them, so `order AB-1` and `item AB-1` shared one entry while this was keyed
    by the id alone (T-061). A surface that declares no entity keys by the id,
    exactly as it did.
    """
    return f"{entity}/{row}" if entity and row else row


def _is_about(structured: object, row: str) -> bool:
    """Whether what came back describes the row that was asked for.

    Checked because `reader_for` can only be as right as the surface's own
    declarations, and a server that mislabels an entity would otherwise have its
    answer cached as a fresh belief about a row it never read. A result that
    names no id is taken at its word: the reader was asked for one row and only
    the server knows its own shape.
    """
    if not isinstance(structured, dict):
        return True
    for name in ("id", "order_id"):
        value = structured.get(name)
        if isinstance(value, str) and value:
            return value == row
    return True


def reads(registry: ToolRegistry, tool: str) -> bool:
    """Whether this tool only looks — so its result may set a belief's age."""
    return _is(registry, tool, SideEffectClass.READ)


__all__ = [
    "HELD",
    "Freshness",
    "Refreshed",
    "cache_key",
    "entity_of",
    "key_of",
    "reads",
    "Run",
    "refresh",
    "refreshed",
    "resume",
    "resumed",
]
