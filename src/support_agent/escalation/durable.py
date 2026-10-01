"""An escalation as a Temporal workflow: raised, held, resolved or lapsed.

T-028, the sibling of `approvals/durable.py` and the same argument one step
larger. A lost approval loses one action; a lost escalation loses a customer
nobody knows is waiting.

What Temporal takes over here is the **lapse**. P-ESC-TTL says a queued
escalation nobody comes to expires, and until now that was a function some
caller had to remember to run: a sweeper on a timer in the demo server, and
nothing at all anywhere else. A conversation on a process that had no sweeper
was held open against a colleague who would never appear. The timer is the
workflow's now, so the rule holds wherever the record lives.

What stays ours is what the rule *means*: who may close an escalation, that an
outcome comes from a declared set, that closing is terminal, and that a lapsed
escalation cannot be resolved an hour later by somebody who never came.

Deterministic code only; time is `workflow.now()`.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, ConfigDict

    from support_agent.contracts import Escalation, EscalationOutcome, EscalationState
    from support_agent.escalation.workflow import refusal


class Raise(BaseModel):
    """A conversation handed to a person: everything the record is made from."""

    model_config = ConfigDict(frozen=True)

    id: str
    conversation_id: str
    run_id: str
    customer_id: str
    reason: str
    rule_id: str
    rules_version: str
    context: str = ""
    tier: int = 1
    ttl_s: int
    queue: str


class Resolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    by: str
    by_customer: str | None = None
    note: str = ""


def _now() -> int:
    return int(workflow.now().timestamp())


def closed_labels(escalation: Escalation) -> dict[str, str]:
    """How an escalation closed, as the labels on `agent.escalations.closed`.

    `outcome` and `rule` since AACP-0044: a desk that keeps closing one rule's
    hand-offs as *the agent could have* is a rule written too broad, and the
    cost of it falls on people who are not the ones tuning it. Both bounded —
    the declared outcomes, and the versioned rule set.
    """
    outcome = escalation.outcome.value if escalation.outcome is not None else "none"
    return {
        "state": escalation.state.value,
        "outcome": outcome,
        "rule": escalation.rule_id or "unattributed",
    }


@workflow.defn
class EscalationWorkflow:
    def __init__(self) -> None:
        self.escalation: Escalation | None = None

    @workflow.run
    async def run(self, raised: Raise) -> Escalation:
        now = _now()
        self.escalation = Escalation(
            id=raised.id,
            conversation_id=raised.conversation_id,
            run_id=raised.run_id,
            customer_id=raised.customer_id,
            tier=raised.tier,
            context=raised.context,
            rule_id=raised.rule_id,
            rules_version=raised.rules_version,
            reason=raised.reason,
            state=EscalationState.QUEUED,
            created_at=now,
            expires_at=now + raised.ttl_s,
        )
        queue = workflow.get_external_workflow_handle(raised.queue)
        await queue.signal(EscalationQueue.opened, [raised.id, raised.conversation_id])
        # The lapse, as a timer rather than as somebody remembering to sweep.
        with contextlib.suppress(TimeoutError):
            await workflow.wait_condition(
                lambda: not self._current.open, timeout=timedelta(seconds=raised.ttl_s)
            )
        if self._current.open:
            self._set(state=EscalationState.EXPIRED, resolved_at=_now())
        # Replay-safe, and the only place a lapse is seen (T-055).
        workflow.metric_meter().create_counter(
            "agent.escalations.closed", "Escalations closed, by how."
        ).add(1, closed_labels(self._current))
        await queue.signal(EscalationQueue.closed, [raised.id, raised.conversation_id])
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self._current

    @workflow.update
    async def resolve(self, decision: Resolution) -> Escalation:
        """A person closes it, and says what it was."""
        self._set(
            state=EscalationState.RESOLVED,
            resolved_at=_now(),
            outcome=EscalationOutcome(decision.outcome),
            outcome_by=decision.by,
            outcome_note=decision.note or None,
        )
        return self._current

    @resolve.validator
    def check(self, decision: Resolution) -> None:
        if self.escalation is None:
            raise ApplicationError("the escalation is not recorded yet", type="EscalationError")
        why = refusal(
            self.escalation,
            outcome=decision.outcome,
            by=decision.by,
            by_customer=decision.by_customer,
            now=_now(),
        )
        if why is not None:
            raise ApplicationError(why, type="EscalationError", non_retryable=True)

    @workflow.query
    def current(self) -> Escalation | None:
        return self.escalation

    @property
    def _current(self) -> Escalation:
        assert self.escalation is not None
        return self.escalation

    def _set(self, **fields: object) -> None:
        self.escalation = self._current.model_copy(update=fields)


@workflow.defn
class EscalationQueue:
    """What a desk sees, and which conversation each escalation belongs to.

    The second question is the one the agent asks every turn — *is anyone
    holding this conversation?* — and it is asked by conversation because that
    is the handle the agent has (F-006).
    """

    def __init__(self) -> None:
        self.open: list[list[str]] = []

    @workflow.run
    async def run(self, open_now: list[list[str]]) -> None:
        self.open = [pair for pair in open_now if pair not in self.open] + self.open
        await workflow.wait_condition(lambda: workflow.info().is_continue_as_new_suggested())
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(self.open)

    @workflow.signal
    def opened(self, pair: list[str]) -> None:
        if pair not in self.open:
            self.open.append(pair)

    @workflow.signal
    def closed(self, pair: list[str]) -> None:
        if pair in self.open:
            self.open.remove(pair)

    @workflow.query
    def listed(self) -> list[str]:
        return [escalation for escalation, _ in self.open]

    @workflow.query
    def on_conversation(self, conversation_id: str) -> str | None:
        """The newest open escalation on this conversation. Two should not
        happen — the entrypoint short-circuits before a second is raised — but
        choosing the last deterministically beats choosing whichever was found
        first."""
        held = [e for e, conversation in self.open if conversation == conversation_id]
        return held[-1] if held else None


__all__ = ["EscalationQueue", "EscalationWorkflow", "Raise", "Resolution", "closed_labels"]
