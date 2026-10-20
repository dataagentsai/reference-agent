"""The `approval` port, named as AHC names it (T-048).

    request      `Approvals.request`: start (or find) the approval for an action
                 and return it once assessed: carried out, waiting, or failed
    await        `settled`: wait for a decision within a declared time, and say
                 what it became (`await` is Python's own word, hence the name)
    stop_signal  `Desk.stop`: withdraw a waiting approval from outside the run;
                 it settles `withdrawn` and is never carried out

The agent holds `Approvals` and nothing else, so a run cannot grant its own
approval (`approval/no_self_grant`). `decide` and `stop` are the desk's, handed
to a person's surface and never to the agent. Whose engine runs the wait (DBOS,
Temporal, a framework's interrupt) is the realization's; the cases in
`approvals.conformance` hold every one of them to the same behaviour.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from agent_harness.approvals.durable import FINAL
from agent_harness.contracts import Approval, ApprovalRecords, ApprovalState


class Desk(Protocol):
    """A person's handle on an approval: decide it, or stop it."""

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        by: str,
        by_customer: str | None = None,
        now: int | None = None,
    ) -> Approval: ...

    async def stop(self, approval_id: str, *, by: str, because: str = "") -> Approval: ...


async def settled(
    approvals: ApprovalRecords, approval_id: str, *, within_s: float, poll_s: float = 0.1
) -> Approval | None:
    """The port's `await`: the approval once nothing is running on its behalf
    (final, or waiting for a person), or as it stands when `within_s` passes.
    `None` when there is no such approval."""
    deadline = time.monotonic() + within_s
    while True:
        found = await approvals.get(approval_id)
        resting = found is not None and (
            found.state in FINAL or (found.state is ApprovalState.WAITING and not found.decided)
        )
        if resting or time.monotonic() >= deadline:
            return found
        await asyncio.sleep(poll_s)


__all__ = ["Desk", "settled"]
