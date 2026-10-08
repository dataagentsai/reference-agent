"""DBOS on PostgreSQL: what the approval and escalation waits share (T-099).

The Azure stack binds `workflow` and `approval` to DBOS: durable waits as a
library on the PostgreSQL the stack already runs, rather than a server of their
own. `approvals/dbos.py` and `escalation/dbos.py` are the two waits; this is the
part of DBOS both of them use, so neither carries its own copy:

- `launch` and `shutdown`: one DBOS per process, its system tables in the
  database the URL names. Every DBOS wait module is imported before `launch`,
  because DBOS registers workflows at import and recovers them at launch.
- `now`: the clock, read in a step, so a recovered workflow sees the moment it
  first read rather than the moment it was replayed — `workflow.now()`'s job.
- The **ballot box**: a decision goes to a waiting workflow as a DBOS message,
  and the workflow — not the caller — runs our rule on it and answers through
  an event the caller is waiting on. Temporal's update validator did this in
  one call; here it is a message and a reply, and the rule is still ours and
  still runs inside the wait, so a desk in another language meets it too.

Only this module and the two waits import `dbos` (an import contract).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, cast

from dbos import DBOS, DBOSConfig, SetWorkflowID, StepOptions
from dbos import error as dbos_error
from opentelemetry import metrics

from agent_harness.contracts.failures import AgentFailure, Fault

_clock: Callable[[], float] = time.time
POLL_S = 1.0
"""How long a caller waits on one read of a reply before checking that the
workflow it is waiting on is still running."""


RUNNING = ("PENDING", "ENQUEUED")
"""A wait still to be answered. `ENQUEUED` is one being recovered: a launch puts
the waits it finds on DBOS's internal queue, and they run from there."""


class WaitUnreachable(AgentFailure):
    """The wait did not answer in time: the database is slow or nothing is
    running the workflow. Trying again is sensible; the decision was not lost,
    because a message once sent is the workflow's to read."""

    fault = Fault.UNREACHABLE


@dataclass(frozen=True)
class Ballot:
    """A decision in an envelope: `nonce` names the reply the sender waits on."""

    nonce: str
    decision: Any


@dataclass(frozen=True)
class Answer:
    """The workflow's reply to one ballot: refused and why, or the record once
    the decision has been acted on."""

    refused: str | None = None
    record: Any = None


def launch(database_url: str, *, name: str = "agent-harness", **config: Any) -> None:
    """Start DBOS on `database_url` (a `postgresql://` URL; its system tables are
    created there on first launch) and recover whatever was waiting."""
    DBOS(config=cast(DBOSConfig, {"name": name, "system_database_url": database_url, **config}))
    DBOS.launch()


def shutdown() -> None:
    """Stop this process's DBOS. Waits stay in the database for the next launch."""
    DBOS.destroy(destroy_registry=False)


def set_clock(clock: Callable[[], float]) -> None:
    """The clock `now` reads. The wall's by default; a test may move it."""
    global _clock
    _clock = clock


async def now() -> float:
    """The time, read once as a step and replayed on recovery."""
    moment: float = await DBOS.run_step_async({"name": "clock.now"}, _read_clock)
    return moment


def _read_clock() -> float:
    return float(_clock())


async def step(
    name: str, fn: Callable[..., Coroutine[Any, Any, Any]], *args: Any, **options: Any
) -> Any:
    """`fn` as a checkpointed step: run once, its result replayed on recovery."""
    return await DBOS.run_step_async(cast(StepOptions, {"name": name, **options}), fn, *args)


async def publish(key: str, value: Any) -> None:
    """Set this workflow's event `key` — how a record is read from outside."""
    await DBOS.set_event_async(key, value)


async def read(workflow_id: str, key: str) -> Any:
    """Another workflow's event `key` as it stands, or `None`, without waiting."""
    return await DBOS.get_event_async(workflow_id, key, timeout_seconds=0)


async def next_ballot(topic: str, within_s: float) -> Ballot | None:
    """The next decision sent to this workflow, or `None` when `within_s` passes."""
    message = await DBOS.recv_async(topic, timeout_seconds=max(0.0, within_s))
    return message if isinstance(message, Ballot) else None


async def reply(ballot: Ballot, answer: Answer) -> None:
    await DBOS.set_event_async(_reply_key(ballot.nonce), answer)


async def refuse_late(topic: str, judge: Callable[[Any], str]) -> None:
    """Answer every ballot still unread when the wait is over, so a desk that
    decided at the last moment is told why rather than left to time out."""
    while (late := await next_ballot(topic, 0)) is not None:
        await reply(late, Answer(refused=judge(late.decision)))


async def deliver(workflow_id: str, topic: str, decision: Any, *, wait_s: float) -> Answer | None:
    """Send a decision to a running wait and return its answer.

    `None` when there is no running wait to answer — never started, or already
    over — so the caller can say which from the record. `WaitUnreachable` when
    the wait is running and did not answer within `wait_s`.
    """
    if not await running(workflow_id):
        return None
    nonce = uuid.uuid4().hex
    try:
        await DBOS.send_async(workflow_id, Ballot(nonce, decision), topic, idempotency_key=nonce)
    except dbos_error.DBOSNonExistentWorkflowError:
        return None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        found = await DBOS.get_event_async(workflow_id, _reply_key(nonce), timeout_seconds=POLL_S)
        if isinstance(found, Answer):
            return found
        if not await running(workflow_id):
            found = await read(workflow_id, _reply_key(nonce))
            return found if isinstance(found, Answer) else None
    raise WaitUnreachable(f"{workflow_id!r} did not answer within {wait_s:.0f}s")


async def running(workflow_id: str) -> bool:
    status = await DBOS.get_workflow_status_async(workflow_id)
    return status is not None and status.status in RUNNING


async def waiting(name: str) -> list[str]:
    """The ids of every running workflow registered as `name`."""
    found = await DBOS.list_workflows_async(
        name=name, status=list(RUNNING), load_input=False, load_output=False
    )
    return [status.workflow_id for status in found]


async def start(
    workflow: Callable[..., Coroutine[Any, Any, Any]], workflow_id: str, *args: Any
) -> None:
    """Start `workflow` under `workflow_id`, or find the one already started:
    the same request asked twice is one wait."""
    with SetWorkflowID(workflow_id):
        await DBOS.start_workflow_async(workflow, *args)


async def count(name: str, labels: dict[str, str]) -> None:
    """Add one to `name`, in a step so a recovered workflow does not count twice."""
    await DBOS.run_step_async({"name": f"count.{name}"}, _add, name, labels)


def _add(name: str, labels: dict[str, str]) -> None:
    metrics.get_meter("agent_harness.waits").create_counter(name).add(1, labels)


def _reply_key(nonce: str) -> str:
    return f"reply:{nonce}"


__all__ = [
    "Answer",
    "Ballot",
    "WaitUnreachable",
    "count",
    "deliver",
    "launch",
    "next_ballot",
    "now",
    "publish",
    "read",
    "refuse_late",
    "reply",
    "running",
    "set_clock",
    "shutdown",
    "start",
    "step",
    "waiting",
]
