"""When the system stops.

L4 · P4. The ReAct loop, hand-written, because a framework owning this layer is
a framework hiding the layer the catalogs exist to expose.

P4 is the only position that can see a *trajectory* — that these fourteen calls
are one runaway task rather than fourteen tasks. Everything enforced here needs
that view, and nothing else has it.

Three terminations that are not "the model said it was done":

**Step budget.** AAC-0055 — hard termination under every condition. An
unexplained stop is indistinguishable from a hang, so the reason is always
recorded.

**Oscillation.** A→B→A→B inside the budget. AAC-0055 catches hard
non-termination and AAC-0054 scores path efficiency against a reference, but
neither catches a loop that repeats itself and then stops in time. That gap is
recorded as G2 against the catalog; this is the implementation that will justify
filling it.

**Provider failure.** A declared degradation path (AAC-0009), returned as a typed
`Failed` rather than escaping as a stack trace.

**Cost ceiling.** Checked between calls, because you cannot un-spend one — so a
single call may overshoot, bounded by `max_output_tokens`. Attribution happens
at P3 where the call is made; the ceiling is here because only P4 can see the
whole task.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from opentelemetry.trace import Span

from support_agent import context as ctx
from support_agent import flow as flw
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent.config import Budgets
from support_agent.contracts import (
    ApprovalRequested,
    Completed,
    Failed,
    IdempotencyKey,
    Identity,
    LLMClient,
    LocalTool,
    Message,
    ModelMalformed,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    NeedsApproval,
    Refused,
    RunId,
    TerminationReason,
    ToolCall,
    ToolClient,
    ToolRegistry,
    ToolResult,
    ToolUnavailable,
    TurnResult,
    Usage,
    new_run_id,
)
from support_agent.cost import Meter
from support_agent.loop import freshness, plan, spend
from support_agent.loop.dispatch import dispatch
from support_agent.loop.screen import Screen


@dataclass
class Trace:
    """What the loop accumulated. Returned alongside the result so a caller can
    assert on the trajectory without reading spans."""

    steps: int = 0
    malformed: int = 0
    usage: Usage = field(default_factory=Usage)
    spend_usd: float = 0.0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    effects: list[tuple[str, str]] = field(default_factory=list)
    """`(operation, record)` for every write the far system confirmed — AHC-0108.

    Separate from `tool_calls`, which is what was *attempted*: a refused
    cancellation and a successful one are the same entry there, and the
    difference is the only part anybody handing this conversation over cares
    about."""
    termination: TerminationReason = TerminationReason.GOAL_REACHED

    def signature_counts(self) -> Counter[tuple[str, str]]:
        return Counter(self.tool_calls)


PASS_ON = "I have not been able to resolve this — let me pass you to a colleague."
IN_CIRCLES = "I am going round in circles on this — let me pass you to a colleague."
TROUBLE = "I am having trouble answering right now."
UNREACHABLE = "I cannot reach our order system right now."

Ended = tuple[TurnResult, Trace]
"""A terminated run: the typed result, and the trajectory that produced it."""


async def run(
    goal: str,
    *,
    identity: Identity,
    llm: LLMClient,
    tools: ToolClient,
    system_prompt: str,
    budgets: Budgets | None = None,
    run_id: RunId | None = None,
    history: tuple[Message, ...] = (),
    oscillation_threshold: int = 3,
    meter: Meter | None = None,
    local_tools: Mapping[str, LocalTool] | None = None,
    policy_rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None = None,
    fan_out: int = flw.DEFAULT_FAN_OUT,
    now: Callable[[], int] | None = None,
    fresh_for_s: int | None = None,
) -> Ended:
    """One task, step by step, until one of the terminations above.

    The loop itself is only this: open the tool surface, take steps until one
    ends the run, and stop on the step budget if none does. What a step *is* —
    ask, account, answer or act — is `_Run`'s, one phase per method.
    """
    budgets = budgets or Budgets()
    run_id = run_id or new_run_id()
    trace = Trace()
    with tel.span(
        "agent.run", **{tel.RUN_ID: run_id, tel.TENANT: identity.customer_id}
    ) as run_span:
        try:
            registry = await tools.list_tools(identity)
        except ToolUnavailable as exc:
            return _failed(run_span, trace, UNREACHABLE, str(exc))

        local = dict(local_tools or {})
        registry = registry.model_copy(
            update={"tools": (*registry.tools, *(t.spec for t in local.values()))}
        )
        this = _Run(
            span=run_span,
            trace=trace,
            identity=identity,
            llm=llm,
            tools=tools,
            registry=registry,
            local_tools=local,
            run_id=run_id,
            system_prompt=system_prompt,
            budgets=budgets,
            meter=meter,
            oscillation_threshold=oscillation_threshold,
            now=now or (lambda: int(time.time())),
            fresh=freshness.Freshness(window_s=fresh_for_s),
            fan_out=fan_out,
            messages=[*history, ctx.user_message(goal)],
            screen=Screen(identity=identity, rules=policy_rules, span=run_span),
        )
        for step in range(budgets.max_steps):
            ended = await this.step(step)
            if ended is not None:
                return ended
        return _stopped(run_span, trace, TerminationReason.STEP_BUDGET_EXHAUSTED, PASS_ON)


@dataclass
class _Run:
    """One run's state, and the phases of a step as methods.

    A method object rather than one long function: each phase is readable on its
    own, and the state they share is named once, here, instead of being threaded
    through a dozen locals.
    """

    span: Span
    trace: Trace
    identity: Identity
    llm: LLMClient
    tools: ToolClient
    registry: ToolRegistry
    local_tools: dict[str, LocalTool]
    run_id: RunId
    system_prompt: str
    budgets: Budgets
    meter: Meter | None
    oscillation_threshold: int
    now: Callable[[], int]
    """The run's clock, injected like every other one here: a module that read
    the wall clock itself would be untestable about time, which is the whole
    subject of `fresh` (AHC L9, F-021)."""
    fresh: freshness.Freshness
    fan_out: int
    messages: list[Message]
    screen: Screen
    seen_results: list[ToolResult] = field(default_factory=list)

    async def step(self, step: int) -> Ended | None:
        """Ask, account, then answer or act. `None` means take another step."""
        self.trace.steps = step + 1
        with tel.span("agent.step", **{tel.STEP: step, tel.RUN_ID: self.run_id}) as step_span:
            response = await self._ask(step_span)
            if not isinstance(response, ModelResponse):
                return response
            self._account(response)

            if not response.wants_tools:
                return self._answer(response)
            if self.meter is not None and self.meter.exceeded:
                return self._stop(TerminationReason.COST_CEILING_REACHED, PASS_ON)

            self.messages.append(
                Message(
                    role="assistant", content=response.text or "", tool_calls=response.tool_calls
                )
            )
            planned = self._plan(response.tool_calls, step)
            if not isinstance(planned, list):
                return planned
            return await self._act(planned)

    async def _ask(self, step_span: Span) -> ModelResponse | Ended:
        """One model call, over the assembled context; its failures become typed ends."""
        # Measured on the way past rather than computed separately: the assembly
        # already knows what it dropped, and asking again would do the work twice.
        built = ctx.assembled(system=self.system_prompt, history=self.messages)
        step_span.set_attribute(tel.CONTEXT_CHARS, built.chars)
        step_span.set_attribute(tel.CONTEXT_EXCHANGES, built.exchanges)
        step_span.set_attribute(tel.CONTEXT_TRIMMED, built.trimmed)
        # Before the call is paid for. The last position where refusing costs
        # nothing (F-027: declared, and until now never reached).
        verdict = self.screen.at(
            pol.Position.PRE_MODEL, tuple(self.seen_results), text=_latest_user_text(self.messages)
        )
        if verdict.blocked:
            return self._stop(TerminationReason.REFUSED, pol.SAFE_REPLY, verdict.rule)

        request = ModelRequest(
            messages=built.messages,
            tools=ctx.model_tools(self.registry),
            max_tokens=self.budgets.max_output_tokens,
        )
        try:
            answered = await self.llm.complete(request)
        except ModelUnavailable as exc:
            tel.counters.model_calls.add(1, {"outcome": "unavailable"})
            return _failed(self.span, self.trace, TROUBLE, str(exc))
        except ModelMalformed as exc:
            # AHC-0001's `parse_failure` decision: fail into a declared shape and
            # **count** the failures. Retrying is deliberately not the answer —
            # the same prompt to the same model produced something unreadable, so
            # a retry mostly buys a second bill. Counted on the span, because a
            # parse-failure rate in a log line is a number nobody ever plots.
            self.trace.malformed += 1
            self.span.set_attribute(tel.MODEL_MALFORMED, self.trace.malformed)
            tel.counters.model_calls.add(1, {"outcome": "malformed"})
            return _failed(self.span, self.trace, TROUBLE, exc.reason)
        tel.counters.model_calls.add(1, {"outcome": "answered"})
        return answered

    def _account(self, response: ModelResponse) -> None:
        """Tokens always; money when a meter is wired."""
        self.trace.usage, spent, cost = spend.account(
            self.trace.usage, response.usage, meter=self.meter
        )
        if spent is not None:
            self.trace.spend_usd = spent
            self.span.set_attribute(tel.COST_CALL_USD, cost)
            self.span.set_attribute(tel.COST_USD, spent)

    def _answer(self, response: ModelResponse) -> Ended:
        """The model is done: its reply passes the output guardrail, or is replaced."""
        verdict = self.screen.at(
            pol.Position.POST_MODEL, tuple(self.seen_results), text=response.text
        )
        if verdict.blocked:
            return self._stop(TerminationReason.REFUSED, pol.SAFE_REPLY, verdict.rule)
        return _completed(self.span, self.trace, response.text)

    def _plan(
        self, calls: tuple[ToolCall, ...], step: int
    ) -> list[tuple[ToolCall, IdempotencyKey]] | Ended:
        """Mint keys and check for oscillation in the order the model emitted the
        calls, before anything runs. Scheduling must not change which call gets
        which key."""
        planned: list[tuple[ToolCall, IdempotencyKey]] = []
        for call in calls:
            made = plan.signature(call.name, call.arguments)
            self.trace.tool_calls.append(made)
            if plan.circling(self.trace.signature_counts(), made, self.oscillation_threshold):
                return self._stop(TerminationReason.OSCILLATION_DETECTED, IN_CIRCLES)
            key = IdempotencyKey(
                run_id=self.run_id, step=step, iteration=len(self.trace.tool_calls)
            )
            planned.append((call, key))
        return planned

    async def _act(self, planned: list[tuple[ToolCall, IdempotencyKey]]) -> Ended | None:
        """Run the calls and feed their results back — or stop for a person.

        An `ApprovalRequested` is the one outcome a tool cannot express as a
        result. The loop does not know which action it was; the signal carries
        what the customer is told.
        """
        # AHC-0107, before the screen and before anything is dispatched: an
        # irreversible action planned against a row this run last read outside
        # its freshness window does not run. The row is read again instead, and
        # what comes back enters context as a result the run must account for —
        # so the model plans again against what is true, and every other control
        # applies to that plan exactly as it applied to the first.
        if await self._refresh(planned):
            return None

        # Screened before anything runs, so a refused call costs nothing and the
        # ones beside it are unaffected.
        refused = self.screen.permitted(planned, tuple(self.seen_results))
        allowed = [pair for pair in planned if pair[0].id not in refused]
        try:
            results = await dispatch(
                self.tools, allowed, self.identity, self.local_tools, self.registry, self.fan_out
            )
        except ApprovalRequested as raised:
            self.trace.termination = TerminationReason.AWAITING_APPROVAL
            self.span.set_attribute(tel.TERMINATION, self.trace.termination.value)
            approval = raised.approval
            waiting = NeedsApproval(
                approval_id=approval.id,
                action=approval.action,
                reason=approval.reason,
                reply=raised.reply,
            )
            return waiting, self.trace

        answered = dict(zip([c.id for c, _ in allowed], results, strict=True))
        for call, _ in planned:
            result = refused.get(call.id) or self.screen.admitted(
                call, answered[call.id], tuple(self.seen_results)
            )
            if not result.is_error and freshness.reads(self.registry, call.name):
                self.fresh.saw(freshness.key_of(call.arguments), self.now(), result.structured)
            if not result.is_error and not freshness.reads(self.registry, call.name):
                # Confirmed by the far system, not claimed by the model. This is
                # the fact half of AHC-0108's fact-versus-claim distinction.
                self.trace.effects.append((call.name, freshness.key_of(call.arguments)))
            self.seen_results.append(result)
            self.messages.append(ctx.tool_message(result, tool_call_id=call.id))
        return None

    async def _refresh(self, planned: list[tuple[ToolCall, IdempotencyKey]]) -> bool:
        """Re-read the rows a stale irreversible action would have acted on."""
        held = await freshness.refresh(
            planned,
            fresh=self.fresh,
            registry=self.registry,
            tools=self.tools,
            identity=self.identity,
            run_id=self.run_id,
            iteration=len(self.trace.tool_calls),
            now=self.now(),
            span=self.span,
        )
        self.messages.extend(held.messages)
        self.seen_results.extend(held.results)
        return bool(held.messages)

    def _stop(self, reason: TerminationReason, message: str, rule_id: str = "") -> Ended:
        return _stopped(self.span, self.trace, reason, message, rule_id)


def _latest_user_text(messages: list[Message]) -> str:
    """What the customer last said — what a `PRE_MODEL` rule is about."""
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _completed(span: Span, trace: Trace, text: str) -> Ended:
    trace.termination = TerminationReason.GOAL_REACHED
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Completed(reply=text), trace


def _stopped(
    span: Span, trace: Trace, reason: TerminationReason, message: str, rule_id: str = ""
) -> Ended:
    """A stop, typed by what stopped it.

    A guardrail block is a **refusal** and says so (F-028, F-026's sibling: that
    fix reached the entrypoint's reply screen and not the loop's own stop, so a
    rule firing inside the loop still surfaced as a success). The other stops —
    a budget spent, a ceiling reached, a loop going in circles — are
    degradations: the turn did what it could and hands on.
    """
    trace.termination = reason
    span.set_attribute(tel.TERMINATION, reason.value)
    if reason is TerminationReason.REFUSED:
        return Refused(reply=message, reason=reason.value, rule_id=rule_id), trace
    return Completed(reply=message, termination=reason), trace


def _failed(span: Span, trace: Trace, customer_message: str, detail: str) -> Ended:
    trace.termination = TerminationReason.UNRECOVERABLE_ERROR
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Failed(customer_message=customer_message, detail=detail), trace


__all__ = ["Trace", "run"]
