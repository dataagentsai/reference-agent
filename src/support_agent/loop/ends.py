"""How a run ends: the trajectory it leaves, and the typed result it returns.

Split from the loop so the loop is only the steps. Every stop goes through one
of three functions here, which is what keeps `termination` on the trace and on
the span in agreement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from opentelemetry.trace import Span

from agent_harness import telemetry as tel
from support_agent.contracts import (
    ActionDeclined,
    ApprovalRequested,
    Completed,
    Failed,
    NeedsApproval,
    Refused,
    TerminationReason,
    TurnResult,
    Usage,
)


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
    reads: list[tuple[str, str]] = field(default_factory=list)
    """`(tool, record)` for every read that answered — what a later turn must
    read again before anything is said about it (AHC-0117)."""
    termination: TerminationReason = TerminationReason.GOAL_REACHED

    def signature_counts(self) -> Counter[tuple[str, str]]:
        return Counter(self.tool_calls)


PASS_ON = "I have not been able to resolve this — let me pass you to a colleague."
UNANSWERED = "I could not deal with everything you raised here."
"""AHC-0118: a concern still not dealt with after the model was sent back once."""
IN_CIRCLES = "I am going round in circles on this — let me pass you to a colleague."
TROUBLE = "I am having trouble answering right now."
UNREACHABLE = "I cannot reach our order system right now."
CALLER_LEFT = "This conversation was closed before I finished — ask again and I will pick it up."
"""Recorded on the conversation for whoever reopens it; nobody was there to read it."""

Ended = tuple[TurnResult, Trace]
"""A terminated run: the typed result, and the trajectory that produced it."""


def completed(span: Span, trace: Trace, text: str) -> Ended:
    trace.termination = TerminationReason.GOAL_REACHED
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Completed(reply=text), trace


def stopped(
    span: Span,
    trace: Trace,
    reason: TerminationReason,
    message: str,
    rule_id: str = "",
    why: str = "",
) -> Ended:
    """A stop, typed by what stopped it.

    A guardrail block is a **refusal** and says so (F-028, F-026's sibling: that
    fix reached the entrypoint's reply screen and not the loop's own stop, so a
    rule firing inside the loop still surfaced as a success). The other stops —
    a budget spent, a ceiling reached, a loop going in circles — are
    degradations: the turn did what it could and hands on.

    `why` is the rule's own explanation. Without it a refusal inside the loop
    said only "refused", where the same rule on the reply said why (F-066,
    AHC-0018: the decision is recorded with its reason).
    """
    trace.termination = reason
    span.set_attribute(tel.TERMINATION, reason.value)
    if reason is TerminationReason.REFUSED:
        return Refused(reply=message, reason=why or reason.value, rule_id=rule_id), trace
    return Completed(reply=message, termination=reason), trace


def handed_on(span: Span, trace: Trace, raised: ApprovalRequested | ActionDeclined) -> Ended:
    """A tool stopped the run for someone else to act: a person deciding an
    approval, or — T-095 — a far end that refused for good, typed `declined` so
    the Tier 2 rule of that name fetches a person instead of the model choosing
    words like "try again later"."""
    if isinstance(raised, ApprovalRequested):
        trace.termination = TerminationReason.AWAITING_APPROVAL
        span.set_attribute(tel.TERMINATION, trace.termination.value)
        approval = raised.approval
        return (
            NeedsApproval(
                approval_id=approval.id,
                action=approval.action,
                reason=approval.reason,
                reply=raised.reply,
            ),
            trace,
        )
    trace.termination = TerminationReason.DECLINED
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    declined = Failed(
        customer_message=raised.reply, detail=raised.detail, termination=trace.termination
    )
    return declined, trace


def failed(span: Span, trace: Trace, customer_message: str, detail: str) -> Ended:
    trace.termination = TerminationReason.UNRECOVERABLE_ERROR
    span.set_attribute(tel.TERMINATION, trace.termination.value)
    return Failed(customer_message=customer_message, detail=detail), trace


__all__ = [
    "CALLER_LEFT",
    "IN_CIRCLES",
    "PASS_ON",
    "TROUBLE",
    "UNANSWERED",
    "UNREACHABLE",
    "Ended",
    "Trace",
    "completed",
    "handed_on",
    "failed",
    "stopped",
]
