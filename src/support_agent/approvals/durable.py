"""An approval as a Temporal workflow: assess, wait for a person or the clock, act.

T-028, adopted rather than built. What we had was a row the agent wrote, a turn
that polled it, and a refund issued by the agent on the customer's next message.
So a granted refund waited for the customer to speak, a wait lived only as long
as whatever process held the clock, and the agent that asked for a refund could
write the record that said it was granted.

Here the wait is Temporal's. The workflow owns every move of the record:

    assess   an activity reads what the action would do; the policy grants what
             the agent may do alone, and everything else waits for a person
    wait     a decision update, or the expiry timer, whichever comes first
    act      a granted action is carried out by an activity, under the key it
             was requested with, the moment it is granted

**What stays ours** is what Temporal cannot know: who may decide (`refusal`,
run as the update's validator so a refused decision never enters history), what
an expired approval means, and the stored key that makes a repeated grant one
effect.

Deterministic code only. Everything this module imports from the package passes
through the sandbox, and time is `workflow.now()`, never the wall.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, ConfigDict

    from support_agent.approvals.workflow import refusal
    from support_agent.contracts import Approval, ApprovalState

ASSESS = "approval.assess"
"""The activity that reads what an action would do and whether a person must
decide it. Named, not imported: which action it is belongs to the agent that
registers it (`approvals/refund.py`), and the workflow stays the same for all."""

CARRY_OUT = "approval.carry_out"

FINAL = frozenset(
    {ApprovalState.DONE, ApprovalState.FAILED, ApprovalState.REFUSED, ApprovalState.EXPIRED}
)
SETTLED = FINAL | {ApprovalState.WAITING}
"""Where a caller may be answered: nothing is running on its behalf."""

TIMEOUT = timedelta(seconds=30)
RETRIES = RetryPolicy(maximum_attempts=3)
"""Three tries. Carrying out repeats safely only because the far end recognises
the stored key; an action without one must not be approvable."""


class Ask(BaseModel):
    """What the agent may put into an approval: the action and whose it is.
    Not the reason, the amount or the decision — those the workflow finds out."""

    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, object]
    customer_id: str
    idempotency_key: str
    ttl_s: int
    queue: str
    """The queue workflow a waiting approval is listed on."""


class Assessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    args: dict[str, object]
    """The arguments as the far end holds them, the amount included (F-014)."""
    reason: str | None
    """Why a person must decide, or `None` when the policy grants it."""
    approver: str = ""
    """Who grants when the policy does, named on the record."""
    failed: str | None = None
    """Why the action could not be assessed at all, told to the model."""


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    granted: bool
    by: str
    by_customer: str | None = None


class CarriedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    text: str


def _now() -> int:
    return int(workflow.now().timestamp())


@workflow.defn
class ApprovalWorkflow:
    def __init__(self) -> None:
        self.approval: Approval | None = None

    @workflow.run
    async def run(self, ask: Ask) -> Approval:
        now = _now()
        self._set(
            approval=Approval(
                id=ask.id,
                action=ask.action,
                args=ask.args,
                reason="being assessed",
                customer_id=ask.customer_id,
                idempotency_key=ask.idempotency_key,
                created_at=now,
                expires_at=now + ask.ttl_s,
                state=ApprovalState.ASSESSING,
            )
        )
        assessed = await workflow.execute_activity(
            ASSESS,
            ask,
            result_type=Assessment,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRIES,
        )
        if assessed.failed is not None:
            return await self._settle(ApprovalState.FAILED, assessed.failed)
        if assessed.reason is None:
            self._set(args=assessed.args, reason="within the automatic limit")
            self._set(decided=True, granted=True, decided_by=assessed.approver)
        else:
            self._set(args=assessed.args, reason=assessed.reason, state=ApprovalState.WAITING)
            await self._wait(ask)
        return await self._act()

    async def _wait(self, ask: Ask) -> None:
        queue = workflow.get_external_workflow_handle(ask.queue)
        await queue.signal(ApprovalQueue.opened, ask.id)
        # The timeout is the approval expiring; what it means is decided below,
        # where "nobody answered" and "somebody answered no" are told apart.
        with contextlib.suppress(TimeoutError):
            await workflow.wait_condition(
                lambda: self._current.decided, timeout=timedelta(seconds=ask.ttl_s)
            )
        await queue.signal(ApprovalQueue.closed, ask.id)

    async def _act(self) -> Approval:
        current = self._current
        if not current.decided:
            return await self._settle(ApprovalState.EXPIRED)
        if not current.granted:
            return await self._settle(ApprovalState.REFUSED)
        self._set(state=ApprovalState.CARRYING_OUT)
        done = await workflow.execute_activity(
            CARRY_OUT,
            self._current,
            result_type=CarriedOut,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRIES,
        )
        state = ApprovalState.DONE if done.ok else ApprovalState.FAILED
        return await self._settle(state, done.text)

    async def _settle(self, state: ApprovalState, result: str | None = None) -> Approval:
        self._set(state=state, result=result)
        # Through Temporal's meter, the one a workflow may use: it is replay-safe,
        # and it is the only place an expiry is ever seen — nobody is there when
        # the timer fires (T-055; the agent's own counter saw only requests).
        workflow.metric_meter().create_counter(
            "agent.approvals.settled", "Approvals settled, by how."
        ).add(1, {"outcome": state.value})
        # A decision waits for the action to finish before it answers, so the
        # run must not end under it.
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self._current

    @workflow.update
    async def decide(self, decision: Decision) -> Approval:
        self._set(decided=True, granted=decision.granted, decided_by=decision.by)
        await workflow.wait_condition(lambda: self._current.state in FINAL)
        return self._current

    @decide.validator
    def check(self, decision: Decision) -> None:
        if self.approval is None:
            raise ApplicationError("the approval is not recorded yet", type="ApprovalError")
        why = refusal(self.approval, by=decision.by, by_customer=decision.by_customer, now=_now())
        if why is not None:
            raise ApplicationError(why, type="ApprovalError", non_retryable=True)

    @workflow.update
    async def assessed(self) -> Approval:
        """The answer a requester waits for: carried out, with a person, or failed."""
        await workflow.wait_condition(
            lambda: self.approval is not None and self.approval.state in SETTLED
        )
        return self._current

    @workflow.query
    def current(self) -> Approval | None:
        return self.approval

    @property
    def _current(self) -> Approval:
        assert self.approval is not None
        return self.approval

    def _set(self, *, approval: Approval | None = None, **fields: object) -> None:
        self.approval = approval or self._current.model_copy(update=fields)


@workflow.defn
class ApprovalQueue:
    """The approvals waiting for a person: what a reviewer's queue lists.

    One long-lived workflow each approval reports to, rather than a visibility
    query, because it answers the same on the development server, the test
    server and a cluster, and a reviewer's queue must not depend on which one
    this is. It continues as new before its history grows large.
    """

    def __init__(self) -> None:
        self.waiting: list[str] = []

    @workflow.run
    async def run(self, waiting: list[str]) -> None:
        # A signal can land before the run starts, so what it added is kept.
        self.waiting = [w for w in waiting if w not in self.waiting] + self.waiting
        await workflow.wait_condition(lambda: workflow.info().is_continue_as_new_suggested())
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(self.waiting)

    @workflow.signal
    def opened(self, approval_id: str) -> None:
        if approval_id not in self.waiting:
            self.waiting.append(approval_id)

    @workflow.signal
    def closed(self, approval_id: str) -> None:
        if approval_id in self.waiting:
            self.waiting.remove(approval_id)

    @workflow.query
    def listed(self) -> list[str]:
        return list(self.waiting)


__all__ = [
    "ASSESS",
    "CARRY_OUT",
    "ApprovalQueue",
    "ApprovalWorkflow",
    "Ask",
    "Assessment",
    "CarriedOut",
    "Decision",
]
