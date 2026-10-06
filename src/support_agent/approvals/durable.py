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
             was requested with, the moment it is granted — or, where what it
             was granted against has moved, settled stale and asked again as a
             fresh approval that a person decides (P-APPROVAL-STALE)

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
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, ConfigDict

    from support_agent.approvals.workflow import refusal
    from support_agent.contracts import Approval, ApprovalState

ASSESS = "approval.assess"
"""The activity that reads what an action would do and whether a person must
decide it. Named, not imported: which action it is belongs to the agent that
registers it (`approvals/refund.py`), and the workflow stays the same for all."""

REMIND = "approval.remind"
"""Told that one is still waiting — the channel's job, done from the workflow
because the workflow is what holds the clock (T-059)."""

CARRY_OUT = "approval.carry_out"

FINAL = frozenset(
    {
        ApprovalState.DONE,
        ApprovalState.FAILED,
        ApprovalState.STALE,
        ApprovalState.REFUSED,
        ApprovalState.EXPIRED,
    }
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
    conversation_id: str = ""
    idempotency_key: str
    ttl_s: int
    remind_before_s: int = 0
    """How long before expiry to say it is still waiting. Zero is no reminder,
    which is what a caller with nowhere to send one should ask for."""
    queue: str
    """The queue workflow a waiting approval is listed on."""
    supersedes: str | None = None
    """The stale grant this asks again (P-APPROVAL-STALE). Set by the workflow
    that went stale, never by the agent: asking again is not a request the
    agent can make, only one a moved order can cause."""
    because: str = ""
    """What moved, in words, so the person deciding again is shown what to
    look at rather than the same request with no sign it was ever granted."""


def asked_again(ask: Ask, moved: str) -> Ask:
    """The same request, to be decided again against the order as it now is.

    Everything the agent asked for is kept — the action, whose it is, the
    conversation, and the **key**: nothing was attempted under it, so it is
    still the one request, and a fresh grant carried out under it is still one
    effect however many times the order moved. The arguments are the ones the
    agent asked with, not the ones the stale assessment filled in, so the
    amount is read again rather than carried across.

    The id counts up from the first (`apr_x`, `apr_x~2`, `apr_x~3`): readable
    as one request asked several times, and deterministic, as a workflow's ids
    must be.
    """
    root, _, times = ask.id.partition("~")
    fresh = f"{root}~{int(times or 1) + 1}"
    return ask.model_copy(update={"id": fresh, "supersedes": ask.id, "because": moved})


class Assessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    args: dict[str, object]
    """The arguments as the far end holds them, the amount included (F-014)."""
    reason: str | None
    """Why a person must decide, or `None` when the policy grants it."""
    approver: str = ""
    """Who grants when the policy does, named on the record."""
    decided_against: dict[str, str] = {}
    """The facts this assessment judged, to be re-read before the action runs
    (F-054). The assessment names them because the assessment is what read
    them; the workflow only carries them across the wait."""
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
    declined: bool = False
    """The far end refused for good (`kind: declined`): not retried, and a
    person arranges another way (P-REFUND-DECLINED)."""
    stale: bool = False
    """The action did not run because what it was decided against had changed.
    Not a failure: nothing was attempted, nothing is half-done, and what the
    approval needs is a person deciding again rather than a retry."""


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
                conversation_id=ask.conversation_id,
                idempotency_key=ask.idempotency_key,
                created_at=now,
                expires_at=now + ask.ttl_s,
                state=ApprovalState.ASSESSING,
                supersedes=ask.supersedes,
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
        self._set(decided_against=assessed.decided_against)
        if assessed.reason is None:
            self._set(args=assessed.args, reason="within the automatic limit")
            self._set(decided=True, granted=True, decided_by=assessed.approver)
        else:
            reason = assessed.reason
            if ask.supersedes is not None:
                reason = f"asked again after {ask.supersedes}, because {ask.because}: {reason}"
            self._set(args=assessed.args, reason=reason, state=ApprovalState.WAITING)
            await self._wait(ask)
        return await self._act(ask)

    async def _wait(self, ask: Ask) -> None:
        queue = workflow.get_external_workflow_handle(ask.queue)
        await queue.signal(ApprovalQueue.opened, ask.id)
        # Two waits, not one: the first ends in a reminder, the second in the
        # expiry. The reminder is the workflow's because the workflow is the
        # only thing awake — the agent's turn ended when it asked, and nobody
        # is watching a clock that runs for a day (T-059).
        remind_after = max(0, ask.ttl_s - ask.remind_before_s)
        if ask.remind_before_s > 0:
            await self._until(remind_after)
            if not self._current.decided:
                await self._remind()
        # The timeout is the approval expiring; what it means is decided below,
        # where "nobody answered" and "somebody answered no" are told apart.
        await self._until(ask.ttl_s - remind_after)
        await queue.signal(ApprovalQueue.closed, ask.id)

    async def _until(self, seconds: int) -> None:
        with contextlib.suppress(TimeoutError):
            await workflow.wait_condition(
                lambda: self._current.decided, timeout=timedelta(seconds=seconds)
            )

    async def _remind(self) -> None:
        """Tell whoever is meant to decide that it is still waiting.

        Best effort on purpose: a notification nobody could deliver must not
        expire an approval or fail a workflow, so a failed reminder is
        swallowed after its retries and the wait carries on to its own end.
        """
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                REMIND,
                self._current,
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRIES,
            )

    async def _act(self, ask: Ask) -> Approval:
        current = self._current
        if not current.decided:
            return await self._settle(ApprovalState.EXPIRED)
        if not current.granted:
            return await self._settle(ApprovalState.REFUSED)
        self._set(state=ApprovalState.CARRYING_OUT)
        try:
            done = await workflow.execute_activity(
                CARRY_OUT,
                self._current,
                result_type=CarriedOut,
                start_to_close_timeout=TIMEOUT,
                retry_policy=RETRIES,
            )
        except ActivityError as exc:
            # A fault that would not clear within the retry bound (T-095): the
            # grant stands unexecuted and says why, rather than the workflow
            # failing with the approval stuck in CARRYING_OUT.
            return await self._settle(ApprovalState.FAILED, f"not carried out: {exc.cause or exc}")
        if done.stale:
            return await self._ask_again(ask, done.text)
        state = ApprovalState.DONE if done.ok else ApprovalState.FAILED
        if done.declined:
            self._set(declined=True)
        return await self._settle(state, done.text)

    async def _ask_again(self, ask: Ask, moved: str) -> Approval:
        """Settle a grant whose facts moved, and ask for a decision on what is
        true now (P-APPROVAL-STALE, AHC-0057 `stale_grant`).

        Asked, not retried: re-assessing into this same record would turn one
        person's decision about one order into standing permission over
        whatever the order became. So the fresh request is its own approval,
        its own workflow and its own reviewer's decision, started before this
        one settles so that `superseded_by` never names something that does
        not exist. It is abandoned rather than owned: this record is final the
        moment it is stale, and the wait that follows can last a day.

        The fresh one is assessed like any other. The order may have moved into
        something a person must decide, or out of anything that may be refunded
        at all — the assessment says which, and this does not second-guess it.
        """
        fresh = asked_again(ask, moved)
        await workflow.start_child_workflow(
            ApprovalWorkflow.run,
            fresh,
            id=fresh.id,
            task_queue=workflow.info().task_queue,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        self._set(superseded_by=fresh.id)
        return await self._settle(ApprovalState.STALE, moved)

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
    "REMIND",
    "CARRY_OUT",
    "ApprovalQueue",
    "ApprovalWorkflow",
    "Ask",
    "Assessment",
    "asked_again",
    "CarriedOut",
    "Decision",
]
