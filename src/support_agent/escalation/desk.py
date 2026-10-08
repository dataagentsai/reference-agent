"""The two handles on the escalation workflow, and the worker that runs it.

`TemporalEscalations` is the agent's: raise one, and ask whether anybody is
holding this conversation. `EscalationDesk` is the reviewer surface's: close
one, with an outcome. Separate types for the same reason approvals have them
(T-028) — the party that raises work for a person should not be able to record
that the person dealt with it.

There are no activities. Raising, holding and lapsing have no effects outside
the record, so this worker runs nothing but the workflow itself — which is why
it needs no login of its own, unlike the approvals worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from agent_harness import telemetry as tel
from support_agent.contracts import Escalation, EscalationOutcome
from support_agent.escalation.durable import (
    EscalationQueue,
    EscalationWorkflow,
    Raise,
    Resolution,
)
from support_agent.escalation.workflow import (
    DEFAULT_TTL_S,
    EscalationError,
    new_escalation_id,
)

TASK_QUEUE = "escalations"

WORKFLOWS = [EscalationWorkflow, EscalationQueue]


@dataclass
class TemporalEscalations:
    """The agent's handle: raise, and read. No outcome, no close."""

    client: Client
    task_queue: str = TASK_QUEUE
    durable: bool = True
    _queue_started: bool = False

    @property
    def queue(self) -> str:
        """The queue workflow's id — see `approvals.TemporalApprovals.queue`:
        distinct from the approval queue's even on a shared task queue."""
        return f"{self.task_queue}.escalation-queue"

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
        """Start the wait, and hand back the record so the caller may speak.

        In this order deliberately. Everything that has gone wrong with this
        path came from telling the customer first and recording second — which
        is to say, never recording at all (AAC-0110).
        """
        await self._ensure_queue()
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
            queue=self.queue,
        )
        with tel.span(
            "agent.escalation.raise",
            **{
                tel.ESCALATION_ID: raised.id,
                tel.ESCALATION_TIER: tier,
                tel.ESCALATION_RULE: rule_id,
                "agent.escalation.rules_version": rules_version,
            },
        ):
            handle = await self.client.start_workflow(
                EscalationWorkflow.run,
                raised,
                id=raised.id,
                task_queue=self.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            recorded = await handle.query(EscalationWorkflow.current)
        assert recorded is not None
        return recorded

    async def get(self, escalation_id: str) -> Escalation | None:
        try:
            return await self.client.get_workflow_handle(escalation_id).query(
                EscalationWorkflow.current
            )
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return None
            raise

    async def open_for(self, conversation_id: str) -> Escalation | None:
        """The unresolved escalation on this conversation, if any."""
        await self._ensure_queue()
        held = await self.client.get_workflow_handle(self.queue).query(
            EscalationQueue.on_conversation, conversation_id
        )
        if held is None:
            return None
        found = await self.get(held)
        return found if found is not None and found.open else None

    async def pending(self) -> tuple[Escalation, ...]:
        """The queue a reviewer sees — P8."""
        await self._ensure_queue()
        listed = await self.client.get_workflow_handle(self.queue).query(EscalationQueue.listed)
        found = [await self.get(i) for i in listed]
        waiting = [e for e in found if e is not None and e.open]
        return tuple(sorted(waiting, key=lambda e: e.created_at))

    async def _ensure_queue(self) -> None:
        if self._queue_started:
            return
        await self.client.start_workflow(
            EscalationQueue.run,
            [],
            id=self.queue,
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        self._queue_started = True


@dataclass(frozen=True)
class EscalationDesk:
    """The colleague's handle: close it, and say what it was."""

    client: Client

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
        """`now` is accepted and not read: the workflow's clock decides whether
        this came too late, so no caller can close a lapsed escalation by
        passing an earlier moment."""
        del now
        decision = Resolution(
            outcome=str(getattr(outcome, "value", outcome)),
            by=by,
            by_customer=by_customer,
            note=note,
        )
        handle = self.client.get_workflow_handle(escalation_id)
        try:
            closed = await handle.execute_update(EscalationWorkflow.resolve, decision)
        except WorkflowUpdateFailedError as exc:
            raise EscalationError(getattr(exc.cause, "message", None) or str(exc)) from None
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise
            raise EscalationError(await self._closed(escalation_id)) from None
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

    async def _closed(self, escalation_id: str) -> str:
        """Why a resolution found no running workflow to take it."""
        try:
            found = await self.client.get_workflow_handle(escalation_id).query(
                EscalationWorkflow.current
            )
        except RPCError:
            found = None
        if found is None:
            return f"no escalation {escalation_id!r}"
        if found.state.value == "expired":
            return f"escalation {escalation_id!r} lapsed before anyone came"
        return f"escalation {escalation_id!r} is already {found.state.value}"


def worker(client: Client, *, task_queue: str = TASK_QUEUE, **extra: Any) -> Worker:
    """The process that holds escalations open. It runs no activities, so it
    needs nothing but the connection."""
    return Worker(
        client,
        task_queue=task_queue,
        **{"workflows": WORKFLOWS, **extra},
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "support_agent", "pydantic", "pydantic_core", "opentelemetry"
            )
        ),
    )


__all__ = ["TASK_QUEUE", "WORKFLOWS", "EscalationDesk", "TemporalEscalations", "worker"]
