"""Our own record of every approval and escalation, beside the wait that owns it.

L14 · L5 (claims-fnol-azure Tier 4a, A3). A wait keeps its state in its engine
— Temporal's history, DBOS's tables — and that is the engine's format, free to
change between versions. A far end that must check a payout was granted read
it there, so swapping the engine broke the money path. These records are ours:
written by the wait, through `ApprovalRecordStore`, in the same step that moves
its state, and read by anyone who must check a decision without knowing which
engine made it.

    approvals    id (= the wait's workflow id), action, args, args_digest,
                 requested_for, decided_by, expires_at, status, timestamps
    escalations  id (= the wait's workflow id), question, found, missing,
                 assignee, sla_due_at, status, timestamps

**The digest binds a decision to one call.** `args_digest` is sha256 over the
canonical JSON of the action, its arguments, whose it is and the key it will be
carried out under. A far end rebuilds it from the call it received and its own
row (the claim, the amount it holds, the policyholder it verified, the key on
the call), so a grant for one claim, amount, person or attempt covers no other.

**Status keeps the code's names.** The design's names, for reading across:

    approvals    assessing, waiting   pending
                 carrying_out, done   approved (done: carried out)
                 refused              rejected
                 expired              expired
                 stale                cancelled (superseded by a fresh ask)
                 failed               (none: assessed or carried out, and failed)
    escalations  queued               open (no `assigned`: nothing sets it yet)
                 resolved             resolved
                 expired              (none: the design re-routes on SLA instead)

**Why not `ApprovalRecords`.** That name already means the far end's reader of
the *engine's* `Approval` (`contracts.protocols`, T-002), which the order system
still uses. These are the stores of our own rows, so they are named for that.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.contracts.human import Escalation
from agent_harness.contracts.tools import Approval, ApprovalState

GRANTED = frozenset({ApprovalState.CARRYING_OUT.value, ApprovalState.DONE.value})
"""The statuses in which a person, or the policy, has granted the action."""


def args_digest(
    action: str, args: Mapping[str, object], *, requested_for: str, idempotency_key: str
) -> str:
    """sha256 over canonical JSON: sorted keys, no spaces, every value as text,
    so `25001` and `"25001"` are one amount whichever side wrote it."""
    canonical = json.dumps(
        {
            "action": action,
            "args": {str(k): str(v) for k, v in args.items()},
            "requested_for": requested_for,
            "idempotency_key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, str] = Field(default_factory=dict)
    args_digest: str
    requested_for: str
    conversation_id: str = ""
    idempotency_key: str
    decided_by: str | None = None
    expires_at: int
    status: str
    reason: str = ""
    created_at: int
    updated_at: int = 0
    """Stamped by the store on each write."""

    @property
    def granted(self) -> bool:
        return self.status in GRANTED


class EscalationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    conversation_id: str
    requested_for: str
    question: str
    found: str = ""
    missing: str = ""
    assignee: str | None = None
    sla_due_at: int
    status: str
    created_at: int
    updated_at: int = 0
    """Stamped by the store on each write."""


@runtime_checkable
class ApprovalRecordReader(Protocol):
    """What a far end is given: read one record, never write one (A4)."""

    async def approval(self, approval_id: str) -> ApprovalRecord | None: ...


@runtime_checkable
class ApprovalRecordStore(ApprovalRecordReader, Protocol):
    """What the wait is given. `put_approval` is an upsert keyed by `id`, so the
    same record written twice — a step re-run after a crash — is one row."""

    async def put_approval(self, record: ApprovalRecord) -> None: ...


@runtime_checkable
class EscalationRecordStore(Protocol):
    """The escalation wait's record, upserted by `id` as approvals are."""

    async def put_escalation(self, record: EscalationRecord) -> None: ...

    async def escalation(self, escalation_id: str) -> EscalationRecord | None: ...


@runtime_checkable
class RecordStore(ApprovalRecordStore, EscalationRecordStore, Protocol):
    """The `records` port's product: both tables, in one database."""


def approval_record(approval: Approval) -> ApprovalRecord:
    """The record of an approval as its wait holds it now."""
    args = {str(k): str(v) for k, v in approval.args.items()}
    return ApprovalRecord(
        id=approval.id,
        action=approval.action,
        args=args,
        args_digest=args_digest(
            approval.action,
            args,
            requested_for=approval.customer_id,
            idempotency_key=approval.idempotency_key,
        ),
        requested_for=approval.customer_id,
        conversation_id=approval.conversation_id,
        idempotency_key=approval.idempotency_key,
        decided_by=approval.decided_by if approval.decided else None,
        expires_at=approval.expires_at,
        status=approval.state.value,
        reason=approval.result or approval.reason,
        created_at=approval.created_at,
    )


def escalation_record(escalation: Escalation, missing: str = "") -> EscalationRecord:
    """The record of an escalation: the question is why it was raised, `found` is
    the hand-off the agent assembled. `missing` is empty until the hand-off
    names its gaps separately (claims-fnol-azure FINDINGS)."""
    return EscalationRecord(
        id=escalation.id,
        conversation_id=escalation.conversation_id,
        requested_for=escalation.customer_id,
        question=escalation.reason,
        found=escalation.context,
        missing=missing,
        assignee=escalation.outcome_by,
        sla_due_at=escalation.expires_at,
        status=escalation.state.value,
        created_at=escalation.created_at,
    )


def refusals(
    record: ApprovalRecord,
    *,
    action: str,
    args: Mapping[str, object],
    requested_for: str,
    idempotency_key: str,
    now: float,
) -> list[str]:
    """Why `record` does not cover this call now; empty when it does.

    The digest is the check: rebuilt from the call as the far end received it,
    it must equal the one the wait recorded. The fields beside it only say
    *which* part differs, so a refusal can name it."""
    found: list[str] = []
    if not record.granted:
        found.append(f"it was not granted (it is {record.status})")
    if now >= record.expires_at:
        found.append("it has expired")
    if args_digest(action, args, requested_for=requested_for, idempotency_key=idempotency_key) == (
        record.args_digest
    ):
        return found
    if record.action != action:
        found.append(f"it approved {record.action}")
    wanted = {str(k): str(v) for k, v in args.items()}
    found += [
        f"it was decided for another {k}" for k in sorted(wanted) if record.args.get(k) != wanted[k]
    ]
    if record.requested_for != requested_for:
        found.append("it is for another person")
    if record.idempotency_key != idempotency_key:
        found.append("it was requested as another call")
    found.append("its args_digest does not match this call")
    return found


__all__ = [
    "GRANTED",
    "ApprovalRecord",
    "ApprovalRecordReader",
    "ApprovalRecordStore",
    "EscalationRecord",
    "EscalationRecordStore",
    "RecordStore",
    "approval_record",
    "args_digest",
    "escalation_record",
    "refusals",
]
