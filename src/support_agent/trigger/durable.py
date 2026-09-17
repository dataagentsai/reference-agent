"""A delivery claim that outlives the process holding it — T-003, on Temporal.

`InMemoryDeliveryLog` answers the obligation for one instance. Two instances
behind a load balancer share nothing, so a webhook redelivered to the other one
starts a second run of the same message, and the ledger cannot see across runs
(the two have different run ids). That is the outside half of AAC-0076, and it
needed a claim both processes can see.

**The claim needs an expiry, and that is the whole difficulty.** `settle` runs in
a `finally`, which does not run when a process is killed outright. In memory the
dictionary dies with the process, so nothing is stranded — self-healing by
accident. A durable row does not die with its writer, and one crash would wedge
that message for ever.

So a claim is a workflow named after the delivery, and the expiry is its own
timer:

    running        somebody is handling it now          → an overlapping run
    completed      it was handled                       → a duplicate
    failed/expired nobody settled it inside the window   → claimable again

Temporal's id-reuse policy does the last line: a new claim may start only where
the previous one did not complete. Nothing sweeps, and no operator has to decide
when a claim is stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from support_agent.trigger.log import (
    Delivery,
    DuplicateDelivery,
    OverlappingRun,
    State,
)

TASK_QUEUE = "deliveries"

CLAIM_TTL_S = 10 * 60
"""How long one turn may hold a claim before it is treated as abandoned.

Long enough for the slowest turn a budget allows, short enough that a killed
process does not silence a customer's message until somebody notices. A claim
that never expired would be worse than no claim at all."""


@workflow.defn
class DeliveryClaim:
    """One delivery, held while a run handles it.

    Deliberately holds nothing but the claim. What the run *does* is the agent's
    business and stays in the agent's process; putting a turn inside a workflow
    would make every model call a replayed activity, which is the design
    `PREFERRED-STACK.md` argued against — the turn stays in our loop, and the
    wait goes on Temporal.
    """

    def __init__(self) -> None:
        self.settled = False

    @workflow.run
    async def run(self, ttl_s: int) -> None:
        try:
            await workflow.wait_condition(lambda: self.settled, timeout=timedelta(seconds=ttl_s))
        except TimeoutError:
            # Failing is the point: a claim nobody settled must be claimable
            # again, and the reuse policy below reads exactly this outcome.
            raise ApplicationError(
                "the run that claimed this delivery never settled", type="ClaimExpired"
            ) from None

    @workflow.signal
    def settle(self) -> None:
        self.settled = True

    @workflow.query
    def held(self) -> bool:
        return not self.settled


WORKFLOWS = [DeliveryClaim]


@dataclass
class TemporalDeliveries:
    """A delivery log two processes can share. Satisfies `DeliveryLog`."""

    client: Client
    task_queue: str = TASK_QUEUE
    ttl_s: int = CLAIM_TTL_S
    durable: bool = True

    def _id(self, delivery_id: str) -> str:
        return f"delivery.{delivery_id}"

    async def claim(self, delivery_id: str) -> Delivery:
        try:
            await self.client.start_workflow(
                DeliveryClaim.run,
                self.ttl_s,
                id=self._id(delivery_id),
                task_queue=self.task_queue,
                # A delivery that was handled may not be handled again; one whose
                # claim expired may.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            raise await self._refusal(delivery_id) from None
        return Delivery(id=delivery_id, state=State.IN_FLIGHT)

    async def settle(self, delivery_id: str) -> None:
        """Release the claim, and wait until it is actually released.

        The wait matters: a signal is delivered asynchronously, so a redelivery
        arriving immediately afterwards would otherwise find the claim still
        running and be called a race rather than the duplicate it is.
        """
        handle = self.client.get_workflow_handle(self._id(delivery_id))
        try:
            await handle.signal(DeliveryClaim.settle)
            await handle.result()
        except WorkflowFailureError:
            # The claim expired while this run was working. Somebody else may
            # have it now, and this caller has nothing to undo.
            pass
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise

    async def state_of(self, delivery_id: str) -> State | None:
        status = await self._status(delivery_id)
        if status is None:
            return None
        if status is WorkflowExecutionStatus.RUNNING:
            return State.IN_FLIGHT
        return State.SETTLED if status is WorkflowExecutionStatus.COMPLETED else None

    async def _refusal(self, delivery_id: str) -> Exception:
        """Which refusal this is — and they are told apart because the operator
        response differs: a redelivery is routine, and two concurrent turns on
        one conversation is a race worth looking at."""
        status = await self._status(delivery_id)
        if status is WorkflowExecutionStatus.RUNNING:
            return OverlappingRun(f"delivery {delivery_id!r} is already running")
        return DuplicateDelivery(f"delivery {delivery_id!r} was already handled")

    async def _status(self, delivery_id: str) -> WorkflowExecutionStatus | None:
        try:
            described = await self.client.get_workflow_handle(self._id(delivery_id)).describe()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return None
            raise
        return described.status


def worker_for(client: Client, *, task_queue: str = TASK_QUEUE, **extra: Any) -> Worker:
    """The process that holds claims. Like the escalation worker it runs no
    activities: a claim has no effect outside itself."""
    return Worker(
        client,
        task_queue=task_queue,
        **{"workflows": WORKFLOWS, "max_cached_workflows": 0, **extra},
    )


__all__ = [
    "CLAIM_TTL_S",
    "TASK_QUEUE",
    "WORKFLOWS",
    "DeliveryClaim",
    "TemporalDeliveries",
    "worker_for",
]
