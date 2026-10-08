"""Typed outcomes. The declared shape of failure, and the router's decision.

L6 — "what the caller can rely on". Every path out of the system is one of these
variants; none of them is an exception escaping to the caller, and none of them
is a bare string.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.contracts.intents import IntentName
from agent_harness.contracts.kinds import TerminationReason


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# --------------------------------------------------------------------------- #
# Route — what the router decides, per user turn, outside the loop.
#
# The router is a pure decision function: it returns one of these and never
# calls the loop. The entrypoint dispatches. Enforced by the `router | loop`
# independence contract rather than by discipline.
#
# Refuse and Escalate never cost a loop iteration. "Give me a discount" and
# "get me a human" resolve deterministically, free, and binary, at stage S2.
# --------------------------------------------------------------------------- #


class Direct(_Frozen):
    """Unambiguous — a deterministic handler answers without the model."""

    kind: Literal["direct"] = "direct"
    intent: IntentName
    handler: str
    args: dict[str, object] = Field(default_factory=dict)


class Agentic(_Frozen):
    """Ambiguous, multi-intent, or unmodelled. The loop is the fallback, not
    the default: when the classifier is unsure, it routes here."""

    kind: Literal["agentic"] = "agentic"
    goal: str
    candidate_intents: tuple[IntentName, ...] = ()


class Refuse(_Frozen):
    """Out of scope. The refusal list in the functional spec is normative —
    "never promise what the system cannot deliver" is only testable against an
    explicit list of things it must not say."""

    kind: Literal["refuse"] = "refuse"
    reason: str
    alternative: str | None = None
    rule_id: str = ""
    """Which rule refused, not just why in prose — the AOAS `refuses` id. Same
    argument as `Escalate.rule_id`: *"which rule refuses most often, and is it
    right to"* cannot be asked of a sentence."""


class Escalate(_Frozen):
    """A human takes over. Triggered by P-ESCALATE, or asked for outright."""

    kind: Literal["escalate"] = "escalate"
    reason: str
    rule_id: str = ""
    """Which rule fired, not just why in prose.

    The reason is for a person reading a trace; this is for grouping. Asking
    *"which rule produces escalations the human said were unnecessary"* is the
    question that tunes the rule set, and it cannot be asked of a sentence.
    """
    tier: int = 1


Route = Annotated[Direct | Agentic | Refuse | Escalate, Field(discriminator="kind")]


# --------------------------------------------------------------------------- #
# TurnResult — what the entrypoint returns. AHC-0010: this is the surface an
# evaluation drives, so every variant must be assertable without parsing prose.
# --------------------------------------------------------------------------- #


class Completed(_Frozen):
    kind: Literal["completed"] = "completed"
    reply: str
    termination: TerminationReason = TerminationReason.GOAL_REACHED


class NeedsApproval(_Frozen):
    """The approval gate returns; it never blocks.

    L14's own question is *what the system does while it waits*: it checkpoints
    to L5 and hands back this result. It promises the customer nothing the
    approver has not yet granted.
    """

    kind: Literal["needs_approval"] = "needs_approval"
    approval_id: str
    action: str
    reason: str
    reply: str
    termination: TerminationReason = TerminationReason.AWAITING_APPROVAL


class Refused(_Frozen):
    kind: Literal["refused"] = "refused"
    reply: str
    reason: str
    rule_id: str = ""
    """The rule that refused: an AOAS `refuses` id on the router's path, a
    guardrail's name when a reply was blocked (F-026)."""
    termination: TerminationReason = TerminationReason.REFUSED


class Escalated(_Frozen):
    """Handed to a person, and — unlike every earlier version of this — written
    down first.

    `ticket_id` is **required**: this result says a person has the conversation,
    and there is no such thing without a record to point at. With no desk wired
    the agent refuses instead (`Refused`), so the type itself now rules out the
    claim F-024 was making.
    """

    kind: Literal["escalated"] = "escalated"
    reply: str
    reason: str
    ticket_id: str
    rule_id: str = ""
    termination: TerminationReason = TerminationReason.AWAITING_HUMAN
    """Present for the same reason every other variant has one: AAC-0055 wants a
    stated reason under every condition, and an escalation used to be the one
    outcome a dashboard grouping by termination silently dropped."""


class Failed(_Frozen):
    """Degradation is a declared path, not an exception — AAC-0009.

    `customer_message` is what the person sees; `detail` is for the operator and
    must never be shown to them.
    """

    kind: Literal["failed"] = "failed"
    customer_message: str
    detail: str
    termination: TerminationReason = TerminationReason.UNRECOVERABLE_ERROR


TurnResult = Annotated[
    Completed | NeedsApproval | Refused | Escalated | Failed,
    Field(discriminator="kind"),
]
