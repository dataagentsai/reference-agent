"""The harness's own vocabulary: what repeating an action costs, and why a loop stopped.

L6. The `SideEffectClass` here is the sharpest thing in the functional spec —
it is what turns L10 idempotency and L14 approval gates from theory into code.
Neither names a domain: every agent's actions cost one of three things to
repeat, and every loop stops for one of these reasons. What an agent can be
*asked* — its intents — and the states its entities pass through are its own,
and live with it.
"""

from __future__ import annotations

from enum import StrEnum


class SideEffectClass(StrEnum):
    """What repeating an action costs.

    READ         — repeating is free and invisible.
    REVERSIBLE   — repeating leaves a trace that can be undone.
    IRREVERSIBLE — repeating charges someone twice. Requires an idempotency key
                   at the tool boundary, and may require human approval.
    """

    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"

    @property
    def requires_idempotency_key(self) -> bool:
        return self is not SideEffectClass.READ


class TerminationReason(StrEnum):
    """Why the loop stopped. Never absent — AAC-0055 requires hard termination
    under every condition, and an unexplained stop is indistinguishable from a
    hang.
    """

    GOAL_REACHED = "goal_reached"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    COST_CEILING_REACHED = "cost_ceiling_reached"
    DEADLINE_REACHED = "deadline_reached"
    """Wall-clock ran out (AHC-0096). Its own value since generation run 1: it
    was reported as a step stop, and the two call for different fixes."""
    OUTPUT_LENGTH_REACHED = "output_length_reached"
    """The model stopped because its output budget ran out (AHC-0025), so what
    it wrote is incomplete. Its own value since generation run 2: sending a
    clipped answer as if it were whole is the failure it names."""
    TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
    """A step planned more tool calls than one step may, or the turn's calls
    would pass what one turn may (AHC-0097). The step's calls do not run: a
    plan cut to its first N would be a partial result nobody marked (AHC-0025)."""
    CALLER_GONE = "caller_gone"
    """The caller left — the connection closed — before the turn finished
    (AHC-0096). Checked where the deadline is, so no further model or tool call
    starts for a reply nobody will read. Not a give-up: nobody is waiting, so
    nobody is paged."""
    OSCILLATION_DETECTED = "oscillation_detected"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_HUMAN = "awaiting_human"
    """A person owns the conversation now.

    Distinct from `AWAITING_APPROVAL`: an approval is a decision about one action
    the agent proposed, and the agent resumes afterwards. This is the whole
    conversation changing hands. Both are "not finished", which is why both
    answer 202 rather than 200 — but a dashboard that could not tell them apart
    would report a handoff as a stalled refund.
    """
    REFUSED = "refused"
    DECLINED = "declined"
    CONCERNS_UNANSWERED = "concerns_unanswered"
    """The model ended a turn with a concern the customer raised not dealt with,
    after being sent back once (AHC-0118, T-093). A person takes it."""
    """A far end refused an action for good — the same answer however often it is
    asked (P-REFUND-DECLINED). Not a failure to retry: a person arranges another way."""
    UNRECOVERABLE_ERROR = "unrecoverable_error"


__all__ = ["SideEffectClass", "TerminationReason"]
