"""The `approval` port's conformance cases, one per invariant key (T-048, tier 2).

As `policy.conformance`: a case names what it checks as `approval/<key>`, the
key AHC's `ports/approval.yaml` gives the invariant. A case drives a `Rig`, which
whoever realizes the port supplies: the handle the agent holds, the desk a person
holds, how many times the action was carried out, and a restart of whatever runs
the wait. Our DBOS and Temporal waits supply one each; a framework's interrupt or
permission hook supplies its own. The file is pinned by digest in the tests.

A refusal is an `ApprovalError` carrying its reason, on every realization: the
desk is told why, and the record does not move.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from agent_harness.approvals.port import Desk, settled
from agent_harness.approvals.workflow import ApprovalError
from agent_harness.contracts import Approval, Approvals, ApprovalState

WITHIN_S = 20.0
"""How long a case waits for a wait to come to rest."""
DAY = 86_400
JUDGED = {"order.total": "12400"}
"""What a rig's assessment must say it judged: `decided_against`."""


class Rig(Protocol):
    agent: Approvals
    """The only handle the agent is given."""
    desk: Desk

    @property
    def carried(self) -> int:
        """How many times the action was carried out, counted at the far end."""
        ...

    @property
    def survives_restart(self) -> bool:
        """Declared: whether a waiting approval outlives what runs it."""
        ...

    async def ask(self, *, ttl_s: int) -> Approval:
        """A request whose assessment needs a person and judges `JUDGED`."""
        ...

    async def restart(self) -> None:
        """Stop whatever runs the waits and start it again."""
        ...


@dataclass(frozen=True)
class Case:
    key: str
    """`approval/<invariant key>`."""
    capability: str
    name: str
    check: Callable[[Rig], Awaitable[None]]


async def _rested(rig: Rig, approval_id: str) -> Approval:
    found = await settled(rig.agent, approval_id, within_s=WITHIN_S)
    assert found is not None, f"no approval {approval_id!r}"
    return found


async def _refused(desk_call: Awaitable[Approval]) -> str:
    try:
        await desk_call
    except ApprovalError as exc:
        return str(exc)
    raise AssertionError("the desk accepted what it had to refuse")


async def _the_agent_cannot_grant_its_own(rig: Rig) -> None:
    assert not hasattr(rig.agent, "decide"), "the agent's handle can decide"
    assert not hasattr(rig.agent, "stop"), "the agent's handle can stop"
    asked = await rig.ask(ttl_s=DAY)
    await _refused(rig.desk.decide(asked.id, granted=True, by=asked.customer_id))
    after = await _rested(rig, asked.id)
    assert (after.state, after.decided, rig.carried) == (ApprovalState.WAITING, False, 0)


async def _a_no_is_an_outcome_with_its_reason(rig: Rig) -> None:
    asked = await rig.ask(ttl_s=DAY)
    answered = await rig.desk.decide(asked.id, granted=False, by="ops-7")
    after = await _rested(rig, asked.id)
    assert answered.state is after.state is ApprovalState.REFUSED, after
    assert after.decided_by == "ops-7" and after.reason, after
    assert rig.carried == 0


async def _nobody_decided_so_it_expired(rig: Rig) -> None:
    asked = await rig.ask(ttl_s=2)
    await asyncio.sleep(3)
    after = await _rested(rig, asked.id)
    assert after.state is ApprovalState.EXPIRED, after
    await _refused(rig.desk.decide(asked.id, granted=True, by="ops-7"))
    assert rig.carried == 0


async def _a_decision_is_recorded_once_with_what_it_judged(rig: Rig) -> None:
    asked = await rig.ask(ttl_s=DAY)
    assert asked.decided_against == JUDGED, asked
    await rig.desk.decide(asked.id, granted=True, by="ops-7")
    await _refused(rig.desk.decide(asked.id, granted=False, by="ops-8"))
    after = await _rested(rig, asked.id)
    assert after.state is ApprovalState.DONE, after
    assert (after.decided_by, after.decided_against) == ("ops-7", JUDGED), after
    assert rig.carried == 1


async def _a_stop_reaches_a_waiting_approval(rig: Rig) -> None:
    asked = await rig.ask(ttl_s=DAY)
    stopped = await rig.desk.stop(asked.id, by="ops-7", because="opened twice")
    after = await _rested(rig, asked.id)
    assert stopped.state is after.state is ApprovalState.WITHDRAWN, after
    assert "ops-7" in (after.result or "") and "opened twice" in (after.result or ""), after
    await _refused(rig.desk.decide(asked.id, granted=True, by="ops-8"))
    assert rig.carried == 0


async def _a_wait_outlives_a_restart_or_says_it_does_not(rig: Rig) -> None:
    assert isinstance(rig.survives_restart, bool)
    if not rig.survives_restart:
        return
    asked = await rig.ask(ttl_s=DAY)
    await rig.restart()
    await rig.desk.decide(asked.id, granted=True, by="ops-7")
    after = await _rested(rig, asked.id)
    assert (after.state, rig.carried) == (ApprovalState.DONE, 1), after


CASES: tuple[Case, ...] = (
    Case(
        "approval/no_self_grant",
        "AHC-0057",
        "the agent holds no decision, and the customer cannot grant their own",
        _the_agent_cannot_grant_its_own,
    ),
    Case(
        "approval/rejection_is_an_outcome",
        "AHC-0080",
        "a person's no is an outcome with its reason, and nothing is carried out",
        _a_no_is_an_outcome_with_its_reason,
    ),
    Case(
        "approval/undecided_expires",
        "AHC-0057",
        "an approval nobody decides expires, and a late grant is refused",
        _nobody_decided_so_it_expired,
    ),
    Case(
        "approval/decision_records_what_it_judged",
        "AHC-0057",
        "a decision is recorded once, with who made it and what it judged",
        _a_decision_is_recorded_once_with_what_it_judged,
    ),
    Case(
        "approval/stop_reaches_running_run",
        "AHC-0054",
        "a stop reaches a waiting approval, which is never carried out",
        _a_stop_reaches_a_waiting_approval,
    ),
    Case(
        "approval/survival_is_declared",
        "AHC-0102",
        "a waiting approval outlives a restart, or the realization says it does not",
        _a_wait_outlives_a_restart_or_says_it_does_not,
    ),
)


__all__ = ["CASES", "DAY", "JUDGED", "WITHIN_S", "Case", "Rig"]
