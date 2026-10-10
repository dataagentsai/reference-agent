"""An approval as a DBOS workflow: the Azure stack's `approval` binding (T-099).

`stacks/azure.yaml` binds `approval` to `dbos-workflows`: *a payout waits for a
handler as a DBOS workflow; the decision is a message our rule validates.* This
is that, with the same moves as `approvals/durable.py` so the two are one
contract on two engines:

    assess   a step runs the agent's assessment; the policy grants what the
             agent may do alone, and everything else waits for a person
    wait     a decision message, or the expiry, whichever comes first; the
             reminder before the expiry is the workflow's, as it is there
    act      a granted action is carried out by a step, retried three times
             and timed out at thirty seconds a try, under the key it was
             requested with; a grant whose facts moved settles stale and is
             asked again as a fresh approval (P-APPROVAL-STALE)

What stays ours is unchanged: `refusal` judges every decision inside the wait
and the sender is answered with its reason, so a refused decision never moves
the record. The steps are the agent's own callables — the same `assess`,
`carry_out` and `remind` its Temporal worker registers as activities — handed
over once with `serve`.

Differences from Temporal, said rather than hidden. Time is the wall's, read in
a step (there is no time-skipping server). An assessment that keeps failing
settles the approval `failed` rather than failing the workflow. The queue a
reviewer sees is DBOS's list of running approval workflows, not a queue
workflow of its own.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from dbos import DBOS

from agent_harness import telemetry as tel
from agent_harness.approvals.desk import approval_id
from agent_harness.approvals.durable import (
    FINAL,
    SETTLED,
    Ask,
    Assessment,
    CarriedOut,
    Decision,
    asked_again,
)
from agent_harness.approvals.workflow import ApprovalError, ApprovalTerms, Terms, refusal
from agent_harness.contracts import Approval, ApprovalState, IdempotencyKey, Identity
from agent_harness.contracts.records import ApprovalRecordStore, approval_record
from agent_harness.state import dbos as box

WORKFLOW = "approval.wait"
TOPIC = "approval.decision"
RECORD = "approval"
ASSESSED = "approval.assessed"


@dataclass(frozen=True)
class Work:
    """What the wait runs as steps: the agent's, as its activities are."""

    assess: Callable[[Ask], Coroutine[Any, Any, Assessment]]
    carry_out: Callable[[Approval], Coroutine[Any, Any, CarriedOut]]
    remind: Callable[[Approval], Coroutine[Any, Any, None]] | None = None
    attempts: int = 3
    retry_interval_s: float = 1.0
    timeout_s: float = 30.0


_work: Work | None = None
_records: ApprovalRecordStore | None = None


def serve(work: Work, records: ApprovalRecordStore | None = None) -> None:
    """Give this process's approval waits their steps, and the store of our own
    records (A3) they write. Before `box.launch`, so a recovered approval finds
    them."""
    global _work, _records
    _work, _records = work, records


def _steps() -> Work:
    if _work is None:
        raise ApprovalError("no approval work is served in this process")
    return _work


@DBOS.workflow(name=WORKFLOW)
async def _wait(ask: Ask) -> Approval:
    return await _Run(ask).run()


class _Run:
    """One approval's moves. Deterministic apart from its steps, as a DBOS
    workflow must be: everything it reads from outside is a step or a message."""

    def __init__(self, ask: Ask) -> None:
        self.ask = ask
        self.approval: Approval | None = None
        self.accepted: box.Ballot | None = None

    @property
    def current(self) -> Approval:
        assert self.approval is not None
        return self.approval

    async def run(self) -> Approval:
        ask, now = self.ask, int(await box.now())
        await self._set(
            Approval(
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
        try:
            assessed = await box.step("approval.assess", _steps().assess, ask, **_retried())
        except Exception as exc:  # noqa: BLE001 — every failure is the record's
            return await self._settle(ApprovalState.FAILED, f"not assessed: {exc}")
        if assessed.failed is not None:
            return await self._settle(ApprovalState.FAILED, assessed.failed)
        if assessed.reason is None:
            await self._update(
                decided_against=assessed.decided_against,
                args=assessed.args,
                reason="within the automatic limit",
                decided=True,
                granted=True,
                decided_by=assessed.approver,
            )
        else:
            reason = assessed.reason
            if ask.supersedes is not None:
                reason = f"asked again after {ask.supersedes}, because {ask.because}: {reason}"
            await self._update(
                decided_against=assessed.decided_against,
                args=assessed.args,
                reason=reason,
                state=ApprovalState.WAITING,
            )
            await self._wait()
        return await self._act()

    async def _wait(self) -> None:
        deadline = self.current.expires_at
        remind_at = deadline - self.ask.remind_before_s if self.ask.remind_before_s > 0 else None
        while not self.current.decided:
            now = await box.now()
            if now >= deadline:
                return
            if remind_at is not None and now >= remind_at:
                remind_at = None
                await self._remind()
                continue
            until = deadline if remind_at is None else min(deadline, remind_at)
            ballot = await box.next_ballot(TOPIC, until - now)
            if ballot is not None:
                await self._consider(ballot)

    async def _consider(self, ballot: box.Ballot) -> None:
        """Our rule on a decision, inside the wait: refused, it is answered and
        the record does not move; accepted, it is answered once acted on."""
        why = self._judge(ballot.decision, int(await box.now()))
        if why is not None:
            await box.reply(ballot, box.Answer(refused=why))
            return
        self.accepted = ballot
        decision: Decision = ballot.decision
        await self._update(decided=True, granted=decision.granted, decided_by=decision.by)

    def _judge(self, decision: object, now: int) -> str | None:
        if not isinstance(decision, Decision):
            return "not a decision"
        return refusal(self.current, by=decision.by, by_customer=decision.by_customer, now=now)

    async def _remind(self) -> None:
        """Best effort, as on Temporal: a reminder nobody could deliver must not
        expire an approval or fail the wait."""
        remind = _steps().remind
        if remind is None:
            return
        with contextlib.suppress(Exception):
            await box.step("approval.remind", remind, self.current, **_retried())

    async def _act(self) -> Approval:
        if not self.current.decided:
            return await self._settle(ApprovalState.EXPIRED)
        if not self.current.granted:
            return await self._settle(ApprovalState.REFUSED)
        await self._update(state=ApprovalState.CARRYING_OUT)
        try:
            done: CarriedOut = await box.step(
                "approval.carry_out", _steps().carry_out, self.current, **_retried()
            )
        except Exception as exc:  # noqa: BLE001 — T-095: the grant stands unexecuted
            return await self._settle(ApprovalState.FAILED, f"not carried out: {exc}")
        if done.stale:
            fresh = asked_again(self.ask, done.text)
            await box.start(_wait, fresh.id, fresh)
            await self._update(superseded_by=fresh.id)
            return await self._settle(ApprovalState.STALE, done.text)
        if done.declined:
            await self._update(declined=True)
        state = ApprovalState.DONE if done.ok else ApprovalState.FAILED
        return await self._settle(state, done.text)

    async def _settle(self, state: ApprovalState, result: str | None = None) -> Approval:
        await self._update(state=state, result=result)
        await box.count("agent.approvals.settled", {"outcome": state.value})
        if self.accepted is not None:
            await box.reply(self.accepted, box.Answer(record=self.current))
        await box.refuse_late(TOPIC, lambda d: self._judge(d, 0) or "already decided")
        return self.current

    async def _update(self, **fields: object) -> None:
        await self._set(self.current.model_copy(update=fields))

    async def _set(self, approval: Approval) -> None:
        was = self.approval
        self.approval = approval
        await _record(approval)
        await box.publish(RECORD, approval)
        if approval.state in SETTLED and (was is None or was.state not in SETTLED):
            await box.publish(ASSESSED, True)


async def _record(approval: Approval) -> None:
    """Our own record (A3), written by a checkpointed step in the move that
    changes the wait's state, before DBOS's event of it.

    A crash between this step and DBOS checkpointing it: recovery replays the
    workflow to here and runs the step again. The write is an upsert keyed by
    the wait's id with the same values, so the second run lands on the same row
    and changes nothing but `updated_at`. A crash after it: the step's result is
    replayed, not re-run, and the event is published. Nothing is carried out
    until the step recording the grant has returned, so a far end never sees a
    payout before the record that covers it."""
    if _records is not None:
        record = approval_record(approval)
        await box.step("approval.record", _records.put_approval, record, **_retried())


def _retried() -> dict[str, object]:
    work = _steps()
    return {
        "retries_allowed": True,
        "max_attempts": work.attempts,
        "interval_seconds": work.retry_interval_s,
        "timeout_seconds": work.timeout_s,
    }


@dataclass
class DBOSApprovals:
    """The agent's handle: request and read. No decision, no write. Satisfies
    `Approvals`, as `TemporalApprovals` does."""

    policy: ApprovalTerms = field(default_factory=Terms)
    durable: bool = True
    wait_s: float = 60.0
    """How long a request waits for its assessment before giving up on it."""

    async def request(
        self,
        *,
        action: str,
        args: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
        conversation_id: str = "",
    ) -> Approval:
        ask = Ask(
            id=approval_id(action, args, idempotency_key),
            action=action,
            args=dict(args),
            customer_id=identity.customer_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key.value,
            ttl_s=self.policy.ttl_s,
            remind_before_s=self.policy.remind_before_s,
            queue=WORKFLOW,
        )
        tel.counters.approvals.add(1, {"outcome": "requested"})
        attributes = {"agent.approval.id": ask.id, "agent.approval.action": action}
        with tel.span("agent.approval.request", **attributes):
            await box.start(_wait, ask.id, ask)
            await DBOS.get_event_async(ask.id, ASSESSED, timeout_seconds=self.wait_s)
            found = await self.get(ask.id)
        if found is None or found.state not in SETTLED:
            raise box.WaitUnreachable(f"approval {ask.id!r} was not assessed in {self.wait_s}s")
        return found

    async def get(self, approval_id: str) -> Approval | None:
        found = await box.read(approval_id, RECORD)
        return found if isinstance(found, Approval) else None

    async def pending(self) -> tuple[Approval, ...]:
        found = [await self.get(i) for i in await box.waiting(WORKFLOW)]
        held = [a for a in found if a is not None and a.state is ApprovalState.WAITING]
        return tuple(sorted(held, key=lambda a: a.created_at))


@dataclass(frozen=True)
class DBOSApprovalDesk:
    """The reviewer's handle: decide. Given to a person's surface, never to the
    agent."""

    wait_s: float = 60.0

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        by: str,
        by_customer: str | None = None,
        now: int | None = None,
    ) -> Approval:
        """A decision, answered once the workflow has acted on it. `now` is not
        read: the wait's clock judges expiry, as on Temporal."""
        del now
        decision = Decision(granted=granted, by=by, by_customer=by_customer)
        attributes = {"agent.approval.id": approval_id, "agent.approval.granted": granted}
        with tel.span("agent.approval.decide", **attributes):
            answer = await box.deliver(approval_id, TOPIC, decision, wait_s=self.wait_s)
        if answer is None:
            raise ApprovalError(await _closed(approval_id))
        if answer.refused is not None:
            raise ApprovalError(answer.refused)
        record: Approval = answer.record
        return record


async def _closed(approval_id: str) -> str:
    """Why a decision found no running wait to take it."""
    found = await DBOSApprovals().get(approval_id)
    if found is None:
        return f"no approval {approval_id!r}"
    if found.state is ApprovalState.EXPIRED:
        return f"approval {approval_id!r} has expired"
    if found.state in FINAL:
        return f"approval {approval_id!r} was already decided"
    return f"approval {approval_id!r} is not waiting for a decision"


__all__ = ["DBOSApprovalDesk", "DBOSApprovals", "Work", "serve"]
