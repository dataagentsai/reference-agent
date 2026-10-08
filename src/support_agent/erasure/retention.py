"""Retention — Q-RETENTION, AAC-0095, T-072.

*Transcripts, traces and every record the harness writes are kept 30 days, then
deleted.* Erasure (`erasure.forget`) asks **whose**; this asks **how old**, and
it keeps erasure's shape: one function over every store, asked in one order,
reporting each store separately so a zero is legible.

**What this reaches and what it does not.** The three stores the harness writes
itself: conversations, the request ledger, stored logins. The rest are kept by
the product that holds them and bounded by its own configuration, in
`compose.yaml`: Prometheus by `--storage.tsdb.retention.time`, Temporal (the
approval and escalation histories) by its namespace retention. Langfuse's
traces are *not* bounded — self-hosted Langfuse offers data retention only in
its Enterprise Edition — and that is recorded as a gap, not papered over here.

**The ledger is deleted, not tombstoned**, where erasure redacts it. Erasure
must keep a settled refund's name because a replay can arrive tomorrow. Age is
different: a delivery name is the channel's, which redelivers for minutes; a
tool name carries a run id, and that run's checkpoint goes in the same pass —
first, so no resumable run ever outlives its guards. What could re-present a
name latest is a granted approval, valid for a day. `SHORTEST_WINDOW_DAYS`
turns that argument into a refusal: a window short enough to reopen a replay is
a misconfiguration, not a setting.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.contracts.failures import AgentFailure, Fault
from support_agent.contracts import CheckpointStore, Requests, SessionStore

DAY_S = 24 * 60 * 60

RETENTION_DAYS = 30
"""The owner's decision of 2026-09-26. `Settings.retention_days` carries it to a
deployment, and the profile threshold `retention_days` states it; a test holds
the three together."""

SHORTEST_WINDOW_DAYS = 7
"""The floor below which expiry could delete a name something may still repeat:
an approval's 24-hour validity, plus a week's margin for a queue that drained
late. Well under the 30 days decided, so it binds only a mistake."""


class WindowTooShort(AgentFailure):
    """A retention window short enough to make a settled call executable again."""

    fault = Fault.MISCONFIGURED


@dataclass(frozen=True)
class Expired:
    """What went, per store — separate counts for erasure's reason: an operator
    reading a daily log needs to see which store did nothing."""

    runs: int
    """Turns deleted: checkpoints last written before the cut-off."""
    names: int
    """Ledger names deleted: answered, or claims that lapsed long ago."""
    sessions: int
    """Stored logins deleted: not refreshed within the window."""
    before: int
    """The cut-off, epoch seconds. Everything older went."""


def cutoff(now: int, days: int = RETENTION_DAYS) -> int:
    """The moment before which a record is too old to keep."""
    if days < SHORTEST_WINDOW_DAYS:
        raise WindowTooShort(
            f"retention of {days} days is under {SHORTEST_WINDOW_DAYS}: a settled call's name "
            "could be deleted while something may still repeat it"
        )
    return now - days * DAY_S


async def expire(
    *,
    now: int,
    checkpoints: CheckpointStore,
    requests: Requests | None = None,
    sessions: SessionStore | None = None,
    days: int = RETENTION_DAYS,
) -> Expired:
    """Delete everything older than the window from every store given.

    `now` is the caller's — the codebase injects clocks, and a daily job that
    read the wall itself could not be tested on day 31. Conversations first,
    then the ledger, for the reason in the module docstring.
    """
    before = cutoff(now, days)
    runs = await checkpoints.expire(before)
    names = await requests.expire(before) if requests is not None else 0
    logins = await sessions.expire(before) if sessions is not None else 0
    return Expired(runs=runs, names=names, sessions=logins, before=before)


__all__ = [
    "DAY_S",
    "RETENTION_DAYS",
    "SHORTEST_WINDOW_DAYS",
    "Expired",
    "WindowTooShort",
    "cutoff",
    "expire",
]
