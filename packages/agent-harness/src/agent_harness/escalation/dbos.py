"""An escalation as a DBOS workflow: raised, held, resolved or lapsed (T-099).

The Azure stack's `workflow` binding for the escalation wait, the sibling of
`approvals/dbos.py` and the same moves as `escalation/durable.py`: the record
is the workflow's, the lapse is its timer, and a resolution is a message our
rule (`escalation.refusal`) judges inside the wait. No steps of the agent's: a
raise, a hold and a lapse have no effect outside the record.

As on Temporal, *is anyone holding this conversation?* is answered by
conversation — here from DBOS's list of running escalation workflows rather
than a queue workflow, the newest one open on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from dbos import DBOS

from agent_harness import telemetry as tel
from agent_harness.contracts import Escalation, EscalationOutcome, EscalationState
from agent_harness.contracts.records import EscalationRecordStore, escalation_record
from agent_harness.escalation.durable import Raise, Resolution, closed_labels
from agent_harness.escalation.workflow import (
    DEFAULT_TTL_S,
    EscalationError,
    new_escalation_id,
    refusal,
)
from agent_harness.state import dbos as box

WORKFLOW = "escalation.wait"
TOPIC = "escalation.resolution"
RECORD = "escalation"

_records: EscalationRecordStore | None = None


def serve(records: EscalationRecordStore | None) -> None:
    """The store of our own records (A3) this process's escalation waits write."""
    global _records
    _records = records


async def _publish(escalation: Escalation) -> None:
    """Our record, then DBOS's event, as `approvals.dbos._record` explains: the
    record's write is a checkpointed step and an upsert keyed by the wait's id,
    so a step re-run after a crash lands on the same row with the same values."""
    if _records is not None:
        await box.step(
            "escalation.record",
            _records.put_escalation,
            escalation_record(escalation),
            retries_allowed=True,
            max_attempts=3,
        )
    await box.publish(RECORD, escalation)


@DBOS.workflow(name=WORKFLOW)
async def _wait(raised: Raise) -> Escalation:
    now = int(await box.now())
    escalation = Escalation(
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
    await _publish(escalation)
    accepted: box.Ballot | None = None
    while escalation.open:
        moment = await box.now()
        if moment >= escalation.expires_at:
            # The lapse, as a timer rather than as somebody remembering to sweep.
            escalation = escalation.model_copy(
                update={"state": EscalationState.EXPIRED, "resolved_at": int(moment)}
            )
            break
        ballot = await box.next_ballot(TOPIC, escalation.expires_at - moment)
        if ballot is None:
            continue
        why = _judge(escalation, ballot.decision, int(await box.now()))
        if why is not None:
            await box.reply(ballot, box.Answer(refused=why))
            continue
        accepted = ballot
        escalation = _resolved(escalation, ballot.decision, int(await box.now()))
    await _publish(escalation)
    await box.count("agent.escalations.closed", closed_labels(escalation))
    if accepted is not None:
        await box.reply(accepted, box.Answer(record=escalation))
    closed = escalation
    await box.refuse_late(TOPIC, lambda d: _judge(closed, d, 0) or "already closed")
    return escalation


def _resolved(escalation: Escalation, decision: Resolution, now: int) -> Escalation:
    return escalation.model_copy(
        update={
            "state": EscalationState.RESOLVED,
            "resolved_at": now,
            "outcome": EscalationOutcome(decision.outcome),
            "outcome_by": decision.by,
            "outcome_note": decision.note or None,
        }
    )


def _judge(escalation: Escalation, decision: object, now: int) -> str | None:
    if not isinstance(decision, Resolution):
        return "not a resolution"
    return refusal(
        escalation,
        outcome=decision.outcome,
        by=decision.by,
        by_customer=decision.by_customer,
        now=now,
    )


@dataclass
class DBOSEscalations:
    """The agent's handle: raise, and read. No outcome, no close."""

    durable: bool = True

    async def raise_for(
        self,
        *,
        conversation_id: str,
        run_id: str,
        customer_id: str,
        reason: str,
        rule_id: str,
        rules_version: str,
        context: str = "",
        tier: int = 1,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> Escalation:
        """Start the wait, and hand back the record so the caller may speak —
        recorded first, as on Temporal (AAC-0110)."""
        raised = Raise(
            id=new_escalation_id(),
            conversation_id=conversation_id,
            run_id=run_id,
            customer_id=customer_id,
            reason=reason,
            rule_id=rule_id,
            rules_version=rules_version,
            context=context,
            tier=tier,
            ttl_s=ttl_s,
            queue=WORKFLOW,
        )
        attributes = {
            tel.ESCALATION_ID: raised.id,
            tel.ESCALATION_TIER: tier,
            tel.ESCALATION_RULE: rule_id,
            "agent.escalation.rules_version": rules_version,
        }
        with tel.span("agent.escalation.raise", **attributes):
            await box.start(_wait, raised.id, raised)
            recorded = await DBOS.get_event_async(raised.id, RECORD, timeout_seconds=60)
        if not isinstance(recorded, Escalation):
            raise box.WaitUnreachable(f"escalation {raised.id!r} was not recorded")
        return recorded

    async def get(self, escalation_id: str) -> Escalation | None:
        found = await box.read(escalation_id, RECORD)
        return found if isinstance(found, Escalation) else None

    async def open_for(self, conversation_id: str) -> Escalation | None:
        """The newest unresolved escalation on this conversation, if any."""
        held = [e for e in await self.pending() if e.conversation_id == conversation_id]
        return held[-1] if held else None

    async def pending(self) -> tuple[Escalation, ...]:
        """The queue a reviewer sees — P8."""
        found = [await self.get(i) for i in await box.waiting(WORKFLOW)]
        held = [e for e in found if e is not None and e.open]
        return tuple(sorted(held, key=lambda e: e.created_at))


@dataclass(frozen=True)
class DBOSEscalationDesk:
    """The colleague's handle: close it, and say what it was."""

    wait_s: float = 60.0

    async def resolve(
        self,
        escalation_id: str,
        *,
        outcome: EscalationOutcome | str,
        by: str,
        by_customer: str | None = None,
        note: str = "",
        now: int | None = None,
    ) -> Escalation:
        """`now` is not read: the wait's clock decides whether this came too late."""
        del now
        decision = Resolution(
            outcome=str(getattr(outcome, "value", outcome)),
            by=by,
            by_customer=by_customer,
            note=note,
        )
        answer = await box.deliver(escalation_id, TOPIC, decision, wait_s=self.wait_s)
        if answer is None:
            raise EscalationError(await _closed(escalation_id))
        if answer.refused is not None:
            raise EscalationError(answer.refused)
        closed: Escalation = answer.record
        with tel.span(
            "agent.escalation.resolve",
            **{
                tel.ESCALATION_ID: closed.id,
                tel.ESCALATION_RULE: closed.rule_id,
                "agent.escalation.outcome": closed.outcome.value if closed.outcome else "",
                "agent.escalation.waited_s": (closed.resolved_at or 0) - closed.created_at,
            },
        ):
            return closed


async def _closed(escalation_id: str) -> str:
    """Why a resolution found no running wait to take it."""
    found = await DBOSEscalations().get(escalation_id)
    if found is None:
        return f"no escalation {escalation_id!r}"
    if found.state is EscalationState.EXPIRED:
        return f"escalation {escalation_id!r} lapsed before anyone came"
    return f"escalation {escalation_id!r} is already {found.state.value}"


__all__ = ["DBOSEscalationDesk", "DBOSEscalations", "serve"]
