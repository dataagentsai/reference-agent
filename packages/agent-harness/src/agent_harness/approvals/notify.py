"""Telling a person an approval is still waiting (T-059).

The workflow holds the clock, so the workflow is what notices; *how* a person
is told is nobody's business here. This module is the seam: an activity the
worker registers, and a `Notifier` behind it that a composition root supplies —
Chatwoot in this deployment, and a pager, an inbox or a chat room in another.

**A doorbell, never a verdict.** Nothing a notifier sends may decide anything:
the message says what is waiting and where it is decided, and the decision
happens at the desk, as a named person, through the rule the workflow holds. A
message that could grant a refund by being clicked is a refund a link scanner
can grant.

**Best effort, and deliberately so.** A reminder that cannot be delivered must
not expire an approval or fail a workflow; the wait is the record, and the
notification is a courtesy on top of it.
"""

from __future__ import annotations

from typing import Protocol

from temporalio import activity

from agent_harness.approvals.durable import REMIND
from agent_harness.contracts import Approval


class Notifier(Protocol):
    """Where a waiting approval is announced."""

    async def waiting(self, approval: Approval) -> None: ...


class Nobody:
    """No notifier wired. The approval still waits, still expires on its timer,
    and nobody is told — which is what a deployment with no channel has, and is
    stated here rather than hidden behind a silent default."""

    async def waiting(self, approval: Approval) -> None:
        return None


class Reminders:
    """The activity the worker registers, bound to one notifier."""

    def __init__(self, notifier: Notifier | None = None) -> None:
        self.notifier = notifier or Nobody()

    @activity.defn(name=REMIND)
    async def remind(self, approval: Approval) -> None:
        await self.notifier.waiting(approval)


def message(approval: Approval, *, desk_url: str = "/ops/desk") -> str:
    """What a person reads. The reference, what it is for, and where to decide
    it — and no link that decides anything."""
    waiting_for = approval.args.get("order_id")
    about = f" for {waiting_for}" if waiting_for else ""
    return (
        f"Still waiting for an approval — {approval.action}{about}.\n"
        f"Reason: {approval.reason}\n"
        f"Reference: {approval.id}\n"
        f"It expires unless somebody decides it. Decide it at the desk ({desk_url}). "
        "Nothing has been done yet."
    )


__all__ = ["Nobody", "Notifier", "Reminders", "message"]
