"""The `approval` port: where an action waits for a person, and a conversation too.

    temporal-updates   approvals and escalations as Temporal workflows, with a
                       worker running the agent's steps (the Open Stack)
    dbos-workflows     the same waits as DBOS workflows on PostgreSQL (Azure)

The product is `Waits`: the agent's handles (request and read; raise and read)
and the desks a person decides through. What runs inside a wait is the agent's:
the hook `work` is called with the worker's own tool connection and returns the
steps (`assess`, `carry_out`, each a Temporal activity where Temporal runs it);
the hook `terms` is how long an approval lives and when it reminds.

When the overlay binds the `records` port (built first), the DBOS waits write
our own record of each approval and escalation through it, in the step that
moves the wait's state (A3). The Temporal waits do not yet: a follow-up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from agent_harness.adapters import Adapter, Setting, Wiring
from agent_harness.contracts import Approvals, Escalations


@dataclass(frozen=True)
class Waits:
    approvals: Approvals
    escalations: Escalations
    approver: Any
    """The approval desk: `decide`. Never handed to the agent."""
    desk: Any
    """The escalation desk: `close`. Never handed to the agent."""


@asynccontextmanager
async def _temporal(wiring: Wiring) -> AsyncIterator[Waits]:
    from agent_harness import approvals as ap
    from agent_harness import escalation as esc

    settings, hooks = wiring.settings, wiring.hooks
    client = hooks.get("client") or await ap.connect_temporal(str(settings["address"]))
    queue, durable = str(settings["task_queue"]), bool(settings["durable"])
    terms = {"policy": hooks["terms"]} if hooks.get("terms") is not None else {}
    async with wiring.built["tool_runtime"].connect() as worker_tools:
        work = hooks["work"](worker_tools)
        running = ap.worker(
            client,
            activities=[work.assess, work.carry_out, *hooks.get("activities", ())],
            task_queue=queue,
            workflows=[*ap.WORKFLOWS, *esc.WORKFLOWS],
        )
        async with running:
            yield Waits(
                ap.TemporalApprovals(client, task_queue=queue, durable=durable, **terms),
                esc.TemporalEscalations(client, task_queue=queue, durable=durable),
                ap.ApprovalDesk(client),
                esc.EscalationDesk(client),
            )


@asynccontextmanager
async def _dbos(wiring: Wiring) -> AsyncIterator[Waits]:
    from agent_harness.approvals import dbos as approvals
    from agent_harness.escalation import dbos as escalations
    from agent_harness.state import dbos as box

    terms = {"policy": wiring.hooks["terms"]} if wiring.hooks.get("terms") is not None else {}
    async with wiring.built["tool_runtime"].connect() as worker_tools:
        work = wiring.hooks["work"](worker_tools)
        # Our own records (A3), when the overlay binds the `records` port.
        records = wiring.built.get("records")
        approvals.serve(approvals.Work(assess=work.assess, carry_out=work.carry_out), records)
        escalations.serve(records)
        box.launch(str(wiring.settings["url"]), name=str(wiring.settings["name"]))
        try:
            yield Waits(
                approvals.DBOSApprovals(**terms),
                escalations.DBOSEscalations(),
                approvals.DBOSApprovalDesk(),
                escalations.DBOSEscalationDesk(),
            )
        finally:
            box.shutdown()
            escalations.serve(None)


TEMPORAL = Adapter(
    "approval",
    "temporal-updates",
    _temporal,
    {
        "address": Setting(),
        "task_queue": Setting(default="approvals"),
        "durable": Setting(default=True),
    },
    hooks=("work",),
)
DBOS = Adapter(
    "approval",
    "dbos-workflows",
    _dbos,
    {"url": Setting(required=True, secret=True), "name": Setting(default="agent-harness")},
    hooks=("work",),
)

__all__ = ["DBOS", "TEMPORAL", "Waits"]
