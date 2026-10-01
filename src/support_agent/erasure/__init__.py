"""Erasure by subject — F-056, and the answer to *"forget what I told you"*.

The finding that produced this module is one sentence from a specification for
a different agent entirely: *a user will ask you to forget something and the
answer cannot be "the vector store does not support that"*. That shape made it
a product requirement. It was already a gap here — three durable stores and one
`DELETE` between them, and that one was logout.

**Not retention.** A schedule that drops rows after ninety days answers a
storage bill. It does not answer a person asking today about a conversation
from last week, and the two want different mechanisms. This is the one that is
a `DELETE`; the time-based one is `erasure.retention` (Q-RETENTION), which
keeps this module's shape and differs on the ledger, for reasons it states.

**One function, not a method on a store**, because the question is about a
person and no single store knows the whole of what is held about them. The
conversation store knows their turns, the ledger knows what those turns did,
the session store knows their login — and nothing joins the three except the
order in which they have to be asked.

That order is the only interesting thing here. The ledger is keyed by run, and
run ids exist only on the conversation rows, so erasing conversations first
destroys the ability to find the ledger rows at all. The stores are therefore
asked in one order and one only, and `Erased` reports each separately so that a
partial result is legible rather than a number that could mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from support_agent.contracts import CheckpointStore, Requests, RunId, SessionStore


@dataclass(frozen=True)
class Erased:
    """What actually went, per store.

    Separate counts rather than a total. A caller answering a person needs to
    be able to say *what* was removed, and an operator reading a log needs to
    see which store did nothing — a zero here is either "they had none" or "we
    asked the wrong question", and a summed total hides both.
    """

    runs: int
    """Turns removed: everything said, in either direction."""
    answers: int
    """Ledger rows whose stored answer was dropped. The rows themselves stay —
    see `Requests.redact`, which is where that argument lives."""
    sessions: int
    """Logins removed. At most one, and zero where they had already logged out."""
    replies: int = 0
    """Saved replies dropped from the delivery store: what a redelivered message
    would have been answered with. Named `customer:key`, so found by customer."""

    @property
    def anything(self) -> bool:
        return bool(self.runs or self.answers or self.sessions or self.replies)


async def forget(
    *,
    customer_id: str,
    checkpoints: CheckpointStore,
    requests: Requests | None = None,
    sessions: SessionStore | None = None,
    subject: str = "",
    deliveries: Requests | None = None,
) -> Erased:
    """Remove what is held about one person, and say what went.

    **The order is the design.** Conversations are erased first and hand back
    the run ids they held; those are what the ledger is keyed by, and nothing
    else in the system can derive them. Asking the ledger first would work too
    — and would leave a window where a turn arriving between the two calls is
    erased from one store and not the other.

    `subject` is separate from `customer_id` because a login and a customer are
    not the same identifier (T-002) and one person can be both. A caller that
    holds only one of them passes only that one, and what cannot be found is
    reported as zero rather than guessed at.

    **Saved replies.** A delivery is named `customer:key` (832c38a), so the
    reply saved for a redelivered message is found by the customer and dropped
    the way a ledger answer is: the name stays, so the message is still not
    answered twice. Until that name carried the customer this module said it
    could not reach them, and once it did nothing asked (F-072).
    """
    runs = await checkpoints.forget(customer_id)
    answers = await requests.redact(runs) if requests is not None and runs else 0
    # `redact` matches a name's first segment, which for a delivery is its
    # customer rather than a run; the type names the ledger's use of it.
    replies = await deliveries.redact((RunId(customer_id),)) if deliveries is not None else 0

    # Read before deleting, because `delete` is idempotent and silent — it
    # cannot tell "there was one" from "there was not", and a report that
    # always says one was removed is a report nobody can trust.
    removed_session = 0
    if sessions is not None and subject and await sessions.get(subject) is not None:
        await sessions.delete(subject)
        removed_session = 1

    return Erased(runs=len(runs), answers=answers, sessions=removed_session, replies=replies)


__all__ = ["Erased", "forget"]
