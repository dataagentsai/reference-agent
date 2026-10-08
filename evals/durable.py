"""Approvals on Temporal's test server, kept on a scenario's clock (T-028).

A scenario moves time by declaring it: twelve hours a turn, a reviewer who comes
after three. The approval workflow judges expiry by its own clock, so the two
must agree or an approval expires on one and not the other. Temporal's
time-skipping test server lets time be moved from outside, and this moves it to
wherever the scenario's clock says before anything reads or writes an approval.

The server is the real one, in memory. Scenarios run the same workflow code a
deployment runs; only what the activities act on is simulated.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from agent_harness import identity as ident
from support_agent import approvals as ap
from support_agent import escalation as esc
from support_agent.contracts import (
    Approval,
    ApprovalState,
    Clock,
    Escalation,
    EscalationOutcome,
    EscalationState,
    IdempotencyKey,
    Identity,
    ToolClient,
)


async def as_customer(customer_id: str) -> Identity:
    """The approvals worker's login in a simulation: the customer's own scopes,
    which the grant elevates for the one call it covers."""
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


ALIGN_LEAD_S = 5
"""How far ahead of the server the scenario's clock starts. More than the real
time the first turn can take before it asks for anything, so the server is
still behind when it does."""


@dataclass
class OnClock:
    """Moves the test server's time to the clock's before every call."""

    env: WorkflowEnvironment
    clock: Clock | None

    async def align(self) -> None:
        """Put the scenario's clock ahead of the server's, once, before turn one.

        The test server starts on the wall clock and runs on in real time, and
        the scenario's clock started earlier, truncated to a whole second. So
        the server was up to about a second *ahead* when the first approval was
        asked, `catch_up` had nothing to do, and the approval was stamped one
        second late or not depending on where the wall clock was inside its
        second. A reviewer due "two turns after" is due on an exact multiple of
        the step, so that one second decided which review pass granted it:
        `the-order-moves-while-a-colleague-decides` failed whenever it fell
        the wrong side (T-076f; measured at diff -1 against 0).

        Moving the scenario's start a few seconds forward is free — where it
        starts is arbitrary — and from then on `catch_up` always has the server
        to move, and lands it on the clock's own second.
        """
        # A stepped scenario clock (AgentTwin's) has a settable start; any
        # other clock is left as it is.
        start = getattr(self.clock, "now", None)
        if not isinstance(start, int) or not hasattr(self.clock, "tick"):
            return
        ahead = int((await self.env.get_current_time()).timestamp()) + ALIGN_LEAD_S
        if start < ahead:
            setattr(self.clock, "now", ahead)  # noqa: B010 — the Protocol has no `now`

    async def catch_up(self, moment: int | None = None) -> None:
        target = moment if moment is not None else (self.clock() if self.clock else None)
        if target is None:
            return
        current = int((await self.env.get_current_time()).timestamp())
        if target > current:
            await self.env.sleep(target - current)


@dataclass
class Approvals:
    """`TemporalApprovals`, on the scenario's clock. Satisfies `Approvals`."""

    inner: ap.TemporalApprovals
    time: OnClock
    durable: bool = False

    async def request(
        self,
        *,
        action: str,
        args: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
        conversation_id: str = "",
    ) -> Approval:
        await self.time.catch_up()
        return await self.inner.request(
            action=action,
            args=args,
            identity=identity,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )

    async def get(self, approval_id: str) -> Approval | None:
        await self.time.catch_up()
        return await self.inner.get(approval_id)

    async def pending(self) -> tuple[Approval, ...]:
        await self.time.catch_up()
        return await self.inner.pending()


@dataclass
class Escalations:
    """`TemporalEscalations`, on the scenario's clock."""

    inner: esc.TemporalEscalations
    time: OnClock
    durable: bool = False

    async def raise_for(self, **fields: object) -> Escalation:
        await self.time.catch_up()
        return await self.inner.raise_for(**fields)  # type: ignore[arg-type]

    async def get(self, escalation_id: str) -> Escalation | None:
        await self.time.catch_up()
        return await self.inner.get(escalation_id)

    async def open_for(self, conversation_id: str) -> Escalation | None:
        await self.time.catch_up()
        return await self.inner.open_for(conversation_id)

    async def pending(self) -> tuple[Escalation, ...]:
        await self.time.catch_up()
        return await self.inner.pending()


@dataclass
class ColleagueDesk:
    """`EscalationDesk`, on the scenario's clock."""

    inner: esc.EscalationDesk
    time: OnClock

    async def resolve(self, escalation_id: str, *, now: int | None = None, **fields: object):
        await self.time.catch_up(now)
        return await self.inner.resolve(escalation_id, **fields)  # type: ignore[arg-type]


def resolving(desk: ColleagueDesk) -> Callable[..., Awaitable[object]]:
    """The desk as AgentTwin's `Desk` calls it: `resolve(store, id, ...)`."""

    async def resolves(_store: object, escalation_id: str, **kwargs: object) -> object:
        return await desk.resolve(escalation_id, **kwargs)

    return resolves


@dataclass
class Desk:
    """`ApprovalDesk`, on the scenario's clock."""

    inner: ap.ApprovalDesk
    time: OnClock

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        by: str,
        by_customer: str | None = None,
        now: int | None = None,
    ) -> Approval:
        await self.time.catch_up(now)
        return await self.inner.decide(approval_id, granted=granted, by=by, by_customer=by_customer)


def decide(desk: Desk) -> Callable[..., Awaitable[Approval]]:
    """The desk as AgentTwin's `Approver` calls it: `decide(store, id, ...)`."""

    async def deciding(_store: object, approval_id: str, **kwargs: object) -> Approval:
        return await desk.decide(approval_id, **kwargs)  # type: ignore[arg-type]

    return deciding


@dataclass
class RememberedEscalations:
    """Escalations a test can write directly, for tests that are not about the
    wait — the counterpart of `Remembered`."""

    records: dict[str, Escalation] = field(default_factory=dict)
    durable: bool = False

    def add(self, escalation: Escalation) -> Escalation:
        self.records[escalation.id] = escalation
        return escalation

    async def raise_for(self, **fields: object) -> Escalation:
        now = int(fields.pop("now", 0))  # type: ignore[arg-type]
        ttl_s = int(fields.pop("ttl_s", esc.DEFAULT_TTL_S))  # type: ignore[arg-type]
        return self.add(
            Escalation(
                id=esc.new_escalation_id(),
                created_at=now,
                expires_at=now + ttl_s,
                **fields,  # type: ignore[arg-type]
            )
        )

    def resolved(self, escalation_id: str, *, by: str, outcome: str = "resolved") -> Escalation:
        """A colleague closed it, as the desk would have."""
        found = self.records[escalation_id]
        return self.add(
            found.model_copy(
                update={
                    "state": EscalationState.RESOLVED,
                    "outcome": EscalationOutcome(outcome),
                    "outcome_by": by,
                }
            )
        )

    def lapse(self, escalation_id: str, *, now: int = 0) -> Escalation:
        """Nobody came, as the workflow's timer would record it."""
        found = self.records[escalation_id]
        return self.add(
            found.model_copy(update={"state": EscalationState.EXPIRED, "resolved_at": now})
        )

    async def get(self, escalation_id: str) -> Escalation | None:
        return self.records.get(escalation_id)

    async def open_for(self, conversation_id: str) -> Escalation | None:
        held = [e for e in self.records.values() if e.conversation_id == conversation_id and e.open]
        return max(held, key=lambda e: e.created_at) if held else None

    async def pending(self) -> tuple[Escalation, ...]:
        queued = [e for e in self.records.values() if e.open]
        return tuple(sorted(queued, key=lambda e: e.created_at))


@dataclass
class Durable:
    env: WorkflowEnvironment
    approvals: Approvals
    desk: Desk
    escalations: Escalations
    colleagues: ColleagueDesk
    task_queue: str

    def worker(
        self,
        tools: ToolClient,
        acting_for: object = None,
        notifier: object = None,
        **kwargs: object,
    ):
        """Another worker on the same server: a restart, from the workflow's side.
        `acting_for` is the worker's login; a real store needs a signed one."""
        login = acting_for or as_customer
        work = ap.RefundWork(tools, acting_for=login, **kwargs)  # type: ignore[arg-type]
        # One worker for both: the approval workflows with their activities, and
        # the escalation workflows, which have none.
        return ap.worker(
            self.env.client,
            activities=[work.assess, work.carry_out, ap.Reminders(notifier).remind],  # type: ignore[arg-type]
            task_queue=self.task_queue,
            # No sticky cache here. A worker that stops still holds its cached
            # workflows on the server until a timeout, and on a test server
            # whose clock only moves when a test moves it that timeout can
            # never fire — so a restart would wait forever for the worker it
            # is replacing.
            max_cached_workflows=0,
            workflows=[*ap.WORKFLOWS, *esc.WORKFLOWS],
        )


@dataclass
class Remembered:
    """Approvals a test can write directly, for tests that are not about the wait.

    The workflow is the only thing that may write a real approval (T-028), which
    is the point of it — so a test about *what a turn says* or *what the far end
    checks* needs a stand-in rather than a server. This is that stand-in, and it
    is deliberately in the evaluation code: nothing the agent ships can reach it.
    """

    records: dict[str, Approval] = field(default_factory=dict)
    durable: bool = False
    asked: list[Approval] = field(default_factory=list)

    def add(self, approval: Approval) -> Approval:
        self.records[approval.id] = approval
        return approval

    async def request(
        self,
        *,
        action: str,
        args: dict[str, object],
        identity: Identity,
        idempotency_key: IdempotencyKey,
        conversation_id: str = "",
        state: ApprovalState = ApprovalState.WAITING,
        now: int = 0,
    ) -> Approval:
        approval = Approval(
            id=ap.approval_id(action, args, idempotency_key),
            action=action,
            args=dict(args),
            reason="above the automatic limit",
            customer_id=identity.customer_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key.value,
            created_at=now,
            expires_at=now + ap.Policy().ttl_s,
            state=state,
        )
        self.asked.append(approval)
        return self.add(approval)

    async def get(self, approval_id: str) -> Approval | None:
        return self.records.get(approval_id)

    async def pending(self) -> tuple[Approval, ...]:
        waiting = [a for a in self.records.values() if a.state is ApprovalState.WAITING]
        return tuple(sorted(waiting, key=lambda a: a.created_at))


@asynccontextmanager
async def server(
    clock: Clock | None = None, policy: ap.Policy | None = None
) -> AsyncIterator[Durable]:
    """A test server and both handles on it, with no worker yet."""
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    async with env:
        queue = f"approvals-{uuid.uuid4().hex[:8]}"
        time = OnClock(env, clock)
        await time.align()
        # The policy here is the *wait's*: how long an approval lives and when
        # it says it is still waiting. The worker's copy is the assessment's.
        extra = {} if policy is None else {"policy": policy}
        inner = ap.TemporalApprovals(env.client, task_queue=queue, durable=False, **extra)
        waits = esc.TemporalEscalations(env.client, task_queue=queue, durable=False)
        yield Durable(
            env=env,
            approvals=Approvals(inner, time),
            desk=Desk(ap.ApprovalDesk(env.client), time),
            escalations=Escalations(waits, time),
            colleagues=ColleagueDesk(esc.EscalationDesk(env.client), time),
            task_queue=queue,
        )


@asynccontextmanager
async def escalations_for(clock: Clock | None = None) -> AsyncIterator[Durable]:
    """A test server holding escalations, which need no activities to run."""
    async with (
        server(clock) as durable,
        esc.worker(durable.env.client, task_queue=durable.task_queue, max_cached_workflows=0),
    ):
        yield durable


@asynccontextmanager
async def approvals_for(
    tools: ToolClient,
    *,
    clock: Clock | None = None,
    policy: ap.Policy | None = None,
    acting_for: object = None,
    notifier: object = None,
) -> AsyncIterator[Durable]:
    """A test server with a worker acting on `tools`."""
    async with server(clock, policy) as durable:
        extra = {} if policy is None else {"policy": policy}
        async with durable.worker(tools, acting_for=acting_for, notifier=notifier, **extra):
            yield durable


__all__ = [
    "Approvals",
    "ColleagueDesk",
    "Escalations",
    "Desk",
    "Durable",
    "OnClock",
    "Remembered",
    "RememberedEscalations",
    "approvals_for",
    "escalations_for",
    "as_customer",
    "decide",
    "resolving",
    "server",
]
