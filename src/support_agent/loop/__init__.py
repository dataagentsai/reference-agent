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

**Tool fan-out** past a step's or a turn's bound stops the turn before any of
the step's calls runs (AHC-0097, `plan.over_fan_out`).

**The caller left.** `gone`, where a door can tell, is asked with the deadline
and before a step's tools run, so no call starts for nobody (AHC-0096).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from opentelemetry.trace import Span

from support_agent import context as ctx
from support_agent import flow as flw
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent.config import Budgets
from support_agent.contracts import (
    ApprovalRequested,
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
    RunId,
    TerminationReason,
    ToolCall,
    ToolClient,
    ToolRegistry,
    ToolResult,
    ToolUnavailable,
    new_run_id,
)
from support_agent.cost import Meter
from support_agent.loop import freshness, plan, spend
from support_agent.loop.dispatch import dispatch
from support_agent.loop.ends import (
    CALLER_LEFT,
    IN_CIRCLES,
    PASS_ON,
    TROUBLE,
    UNREACHABLE,
    Ended,
    Trace,
    completed,
    failed,
    stopped,
)
from support_agent.loop.screen import Screen

Gone = Callable[[], Awaitable[bool]]
"""Whether the caller has left: `/chat`'s `request.is_disconnected` (AHC-0096)."""


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
    gone: Gone | None = None,
    resumed: tuple[str, ...] = (),
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
            return failed(run_span, trace, UNREACHABLE, str(exc))

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
            gone=gone,
        )
        this.started = this.now()
        await freshness.resumed(resumed, this)
        for step in range(budgets.max_steps):
            ended = await this.step(step)
            if ended is not None:
                return ended
        return stopped(run_span, trace, TerminationReason.STEP_BUDGET_EXHAUSTED, PASS_ON)


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
    started: int = 0
    keys: plan.Keys = field(default_factory=plan.Keys)
    gone: Gone | None = None

    async def step(self, step: int) -> Ended | None:
        """Ask, account, then answer or act. `None` means take another step."""
        self.trace.steps = step + 1
        out = await self._out_of_time()
        if out is not None:
            return out
        with tel.span("agent.step", **{tel.STEP: step, tel.RUN_ID: self.run_id}) as step_span:
            response = await self._ask(step_span)
            if not isinstance(response, ModelResponse):
                return response
            self._account(response)

            if response.stop_reason == "length":
                # Clipped, whether it is text or tool calls: neither is sent on.
                return self._stop(TerminationReason.OUTPUT_LENGTH_REACHED, PASS_ON)
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

    async def _out_of_time(self) -> Ended | None:
        """The deadline, and the caller leaving, which ends a turn as surely (AHC-0096)."""
        if self.now() - self.started > self.budgets.max_turn_seconds:
            return self._stop(TerminationReason.DEADLINE_REACHED, PASS_ON)
        if await self._caller_gone():
            return self._stop(TerminationReason.CALLER_GONE, CALLER_LEFT)
        return None

    async def _caller_gone(self) -> bool:
        return self.gone is not None and await self.gone()

    async def _ask(self, step_span: Span) -> ModelResponse | Ended:
        """One model call, over the assembled context; its failures become typed ends."""
        # Measured on the way past rather than computed separately: the assembly
        # already knows what it dropped, and asking again would do the work twice.
        try:
            built = ctx.assembled(system=self.system_prompt, history=self.messages)
        except ctx.BrokenTranscript as exc:
            # A transcript no provider will accept is a typed end like any other
            # failure here. It escaped the loop, and the caller got a plain 500
            # with no reply for the customer (F-065).
            return failed(self.span, self.trace, TROUBLE, str(exc))
        step_span.set_attribute(tel.CONTEXT_CHARS, built.chars)
        step_span.set_attribute(tel.CONTEXT_EXCHANGES, built.exchanges)
        step_span.set_attribute(tel.CONTEXT_TRIMMED, built.trimmed)
        # Before the call is paid for. The last position where refusing costs
        # nothing (F-027: declared, and until now never reached).
        verdict = self.screen.at(
            pol.Position.PRE_MODEL, tuple(self.seen_results), text=_latest_user_text(self.messages)
        )
        if verdict.blocked:
            return self._stop(
                TerminationReason.REFUSED, pol.SAFE_REPLY, verdict.rule, verdict.reason
            )

        request = ModelRequest(
            messages=built.messages,
            tools=ctx.model_tools(self.registry),
            max_tokens=self.budgets.max_output_tokens,
        )
        try:
            answered = await self.llm.complete(request)
        except ModelUnavailable as exc:
            tel.counters.model_calls.add(1, {"outcome": "unavailable"})
            return failed(self.span, self.trace, TROUBLE, str(exc))
        except ModelMalformed as exc:
            # AHC-0001's `parse_failure` decision: fail into a declared shape and
            # **count** the failures. Retrying is deliberately not the answer —
            # the same prompt to the same model produced something unreadable, so
            # a retry mostly buys a second bill. Counted on the span, because a
            # parse-failure rate in a log line is a number nobody ever plots.
            self.trace.malformed += 1
            self.span.set_attribute(tel.MODEL_MALFORMED, self.trace.malformed)
            tel.counters.model_calls.add(1, {"outcome": "malformed"})
            return failed(self.span, self.trace, TROUBLE, exc.reason)
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
            return self._stop(
                TerminationReason.REFUSED, pol.SAFE_REPLY, verdict.rule, verdict.reason
            )
        return completed(self.span, self.trace, response.text)

    def _plan(
        self, calls: tuple[ToolCall, ...], step: int
    ) -> list[tuple[ToolCall, IdempotencyKey]] | Ended:
        """Mint keys and check for oscillation in the order the model emitted the
        calls, before anything runs. Scheduling must not change which call gets
        which key. The fan-out bounds come first: a plan past one is refused
        whole, before a key is minted or a call counted (AHC-0097)."""
        bound = plan.over_fan_out(len(calls), len(self.trace.tool_calls), self.budgets)
        if bound is not None:
            self.span.set_attribute(tel.TOOL_CALL_BOUND, bound)
            return self._stop(TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED, PASS_ON)
        planned: list[tuple[ToolCall, IdempotencyKey]] = []
        for call in calls:
            made = plan.signature(call.name, call.arguments)
            self.trace.tool_calls.append(made)
            if plan.circling(self.trace.signature_counts(), made, self.oscillation_threshold):
                return self._stop(TerminationReason.OSCILLATION_DETECTED, IN_CIRCLES)
            iteration = len(self.trace.tool_calls)
            key = self.keys.mint(made, run_id=self.run_id, step=step, iteration=iteration)
            planned.append((call, key))
        return planned

    async def _act(self, planned: list[tuple[ToolCall, IdempotencyKey]]) -> Ended | None:
        """Run the calls and feed their results back — or stop for a person.

        An `ApprovalRequested` is the one outcome a tool cannot express as a
        result. The loop does not know which action it was; the signal carries
        what the customer is told.
        """
        # The tools have not started, and need not start for nobody (AHC-0096).
        if await self._caller_gone():
            return self._stop(TerminationReason.CALLER_GONE, CALLER_LEFT)
        # AHC-0107, before the screen and before anything is dispatched: an
        # irreversible action planned against a row this run last read outside
        # its freshness window does not run. The row is read again instead, and
        # what comes back enters context as a result the run must account for —
        # so the model plans again against what is true, and every other control
        # applies to that plan exactly as it applied to the first.
        if await freshness.refreshed(planned, self):
            return None

        # Screened before anything runs, so a refused call costs nothing and the
        # ones beside it are unaffected.
        refused = self.screen.permitted(planned, tuple(self.seen_results))
        allowed = [pair for pair in planned if pair[0].id not in refused]
        # A read is true of some moment between asking and answering; asking is
        # the only bound a freshness window can be measured from (AHC-0107).
        asked = self.now()
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
            if not result.is_error:
                self.keys.settled(plan.signature(call.name, call.arguments))
                if freshness.reads(self.registry, call.name):
                    self.fresh.remember(self.registry, call, asked, result.structured)
                    self.trace.reads.append((call.name, freshness.key_of(call.arguments)))
                else:
                    # Confirmed by the far system, not claimed by the model — AHC-0108's fact half.
                    self.trace.effects.append((call.name, freshness.key_of(call.arguments)))
            self.seen_results.append(result)
            self.messages.append(ctx.tool_message(result, tool_call_id=call.id))
        return None

    def _stop(
        self, reason: TerminationReason, message: str, rule_id: str = "", why: str = ""
    ) -> Ended:
        return stopped(self.span, self.trace, reason, message, rule_id, why)


def _latest_user_text(messages: list[Message]) -> str:
    """What the customer last said — what a `PRE_MODEL` rule is about."""
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


__all__ = ["Gone", "Trace", "run"]
