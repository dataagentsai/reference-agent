"""The two handles on the approval workflow, and the worker that runs it.

`TemporalApprovals` is what the agent is given: it can ask for an approval and
read one. `ApprovalDesk` is what a reviewer is given: it can decide. They are
separate types on purpose (T-028, carried from T-002). The far end's check of an
approval is only as independent as the approvals themselves, and while the agent
held a store with `put`, it could have written its own grant.

Both are thin. A decision the rules refuse is refused by the workflow's
validator, not here, so a desk written in another language is held to the same
rules.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from support_agent import telemetry as tel
from support_agent.approvals.durable import (
    ApprovalQueue,
    ApprovalWorkflow,
    Ask,
    Decision,
)
from support_agent.approvals.policy import Policy
from support_agent.approvals.workflow import ApprovalError
from support_agent.contracts import Approval, ApprovalState, IdempotencyKey, Identity

TASK_QUEUE = "approvals"


async def connect_temporal(address: str, *, namespace: str = "default") -> Client:
    """A client that carries the contracts as they are: pydantic, frozen."""
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )


def approval_id(action: str, args: dict[str, object], key: IdempotencyKey) -> str:
    """The same call asked twice is the same approval.

    Derived from the key the call was requested under, the action and its
    arguments, so a retried request finds the workflow the first one started,
    and two different refunds in one run are two approvals.
    """
    basis = "|".join([key.value, action, *(f"{k}={args[k]}" for k in sorted(args))])
    return f"apr_{hashlib.sha256(basis.encode()).hexdigest()[:12]}"


@dataclass
class TemporalApprovals:
    """The agent's handle: request and read. No decision, no write."""

    client: Client
    task_queue: str = TASK_QUEUE
    policy: Policy = field(default_factory=Policy)
    durable: bool = True
    """Whether the server keeps a wait across its own restart. A development
    server on a volume does; the test server does not, and says so."""
    _queue_started: bool = False

    @property
    def queue(self) -> str:
        return f"{self.task_queue}.queue"

    async def request(
        self,
        *,
        action: str,
        args: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
    ) -> Approval:
        await self._ensure_queue()
        ask = Ask(
            id=approval_id(action, args, idempotency_key),
            action=action,
            args=dict(args),
            customer_id=identity.customer_id,
            idempotency_key=idempotency_key.value,
            ttl_s=self.policy.ttl_s,
            queue=self.queue,
        )
        tel.counters.approvals.add(1, {"outcome": "requested"})
        attributes = {"agent.approval.id": ask.id, "agent.approval.action": action}
        with tel.span("agent.approval.request", **attributes):
            try:
                handle = await self.client.start_workflow(
                    ApprovalWorkflow.run,
                    ask,
                    id=ask.id,
                    task_queue=self.task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
            except WorkflowAlreadyStartedError:
                handle = self.client.get_workflow_handle(ask.id)
            try:
                return await handle.execute_update(ApprovalWorkflow.assessed)
            except RPCError as exc:
                # Already finished: the same request was answered before.
                found = await self.get(ask.id) if _gone(exc) else None
                if found is None:
                    raise
                return found

    async def get(self, approval_id: str) -> Approval | None:
        try:
            return await self.client.get_workflow_handle(approval_id).query(
                ApprovalWorkflow.current
            )
        except RPCError as exc:
            if _gone(exc):
                return None
            raise

    async def pending(self) -> tuple[Approval, ...]:
        await self._ensure_queue()
        listed = await self.client.get_workflow_handle(self.queue).query(ApprovalQueue.listed)
        found = [await self.get(i) for i in listed]
        waiting = [a for a in found if a is not None and a.state is ApprovalState.WAITING]
        return tuple(sorted(waiting, key=lambda a: a.created_at))

    async def _ensure_queue(self) -> None:
        if self._queue_started:
            return
        await self.client.start_workflow(
            ApprovalQueue.run,
            [],
            id=self.queue,
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        self._queue_started = True


@dataclass(frozen=True)
class ApprovalDesk:
    """The reviewer's handle: decide. Given to a person's surface, never to
    the agent."""

    client: Client

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        by: str,
        by_customer: str | None = None,
        now: int | None = None,
    ) -> Approval:
        """A decision, answered once the workflow has acted on it.

        `now` is accepted and not read: the workflow's clock judges expiry, so
        a caller cannot decide an approval into or out of its window.
        """
        del now
        decision = Decision(granted=granted, by=by, by_customer=by_customer)
        attributes = {"agent.approval.id": approval_id, "agent.approval.granted": granted}
        with tel.span("agent.approval.decide", **attributes):
            handle = self.client.get_workflow_handle(approval_id)
            try:
                return await handle.execute_update(ApprovalWorkflow.decide, decision)
            except WorkflowUpdateFailedError as exc:
                cause = exc.cause
                raise ApprovalError(getattr(cause, "message", None) or str(exc)) from None
            except RPCError as exc:
                if not _gone(exc):
                    raise
                raise ApprovalError(await self._closed(approval_id)) from None

    async def _closed(self, approval_id: str) -> str:
        """Why a decision found no running workflow to take it."""
        try:
            found = await self.client.get_workflow_handle(approval_id).query(
                ApprovalWorkflow.current
            )
        except RPCError:
            found = None
        if found is None:
            return f"no approval {approval_id!r}"
        if found.state is ApprovalState.EXPIRED:
            return f"approval {approval_id!r} has expired"
        return f"approval {approval_id!r} was already decided"


def worker(
    client: Client,
    *,
    activities: Sequence[Callable[..., object]],
    task_queue: str = TASK_QUEUE,
    **extra: Any,
) -> Worker:
    """The process that runs approvals. Its activities act under their own
    login, so whoever runs this holds the elevated path and the agent does not."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ApprovalWorkflow, ApprovalQueue],
        activities=list(activities),
        **extra,
        # The package passes through the sandbox rather than being re-imported
        # per workflow: its modules are deterministic where a workflow uses
        # them, and pydantic and OpenTelemetry fail to load twice.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "support_agent", "pydantic", "pydantic_core", "opentelemetry"
            )
        ),
    )


def _gone(exc: RPCError) -> bool:
    """No running workflow answered: never started, or already finished."""
    return exc.status is RPCStatusCode.NOT_FOUND


__all__ = [
    "TASK_QUEUE",
    "ApprovalDesk",
    "TemporalApprovals",
    "approval_id",
    "connect_temporal",
    "worker",
]
