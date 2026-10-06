"""AHC-0108 — what this conversation is about, beside what was said about it.

The transcript is a record of *sentences*. Every question anybody later asks of
a piece of work — which order is this about, what has actually been done, what is
somebody still waiting on — is answerable from it only by reading prose the model
wrote, and only for as long as the prose survives.

Reduction is where that bites. What compaction discards is the early part of a
conversation, which is where the identifier everything since has been about was
first mentioned; and whatever it discards was the only copy. A handoff assembled
by summarising a transcript inherits that loss, plus the summariser's errors, and
the person receiving it cannot tell quotation from paraphrase.

## Written from what happened, never from what was said

Every value here is put there by the harness at the moment it did something. An
identifier that reached a tool is a **fact**; the same identifier in a sentence
is a **claim**. The two can disagree, and the disagreement is worth having —
it is exactly what the truthfulness screen reads, and a record derived from the
transcript could not produce it because it would be made of the claims.

## Bounded by kind, not by age

Nothing here is a list that grows with the conversation. What was asked is one
value. The records in play and the actions landed are sets, so a customer who
asks about the same order nine times costs one entry. What is outstanding is
whatever is genuinely outstanding, which is small — or the system has a problem
this record is not the place to solve.

So there is no oldest entry to drop, which is what lets the transcript be
reduced while this is not.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from support_agent.contracts import Escalated, NeedsApproval, TurnResult


class Facts(BaseModel):
    """The work, as the harness knows it rather than as it was described.

    Frozen, like everything else that survives a turn: a record somebody can
    edit in place is a record whose history is the last write.
    """

    model_config = ConfigDict(frozen=True)

    asked: str = ""
    """What the customer last asked for, in their own words.

    Theirs rather than the model's paraphrase, and the latest rather than the
    first: a conversation that has moved on is about the thing it moved on to,
    and the person picking up a handoff needs the current question, not the
    opening one.
    """

    concerns: tuple[str, ...] = ()
    """Each thing the latest message raised, in the customer's words — one per
    clause (AHC-0118, T-093). What a person picking this up must see all of,
    because the transcript's next turn is about whichever one was answered."""

    records: tuple[str, ...] = ()
    """The identifiers that reached a tool. Sorted, so two runs that touched the
    same rows in a different order produce the same record — a handoff that
    differed by scheduling would be one nobody could compare."""

    read: tuple[str, ...] = ()
    """The reads that answered, as `tool:record`. What the conversation has
    looked at, and therefore what a later turn must look at again before it is
    spoken of: a read from an earlier turn has no age this turn can trust, and
    after a pause of days it is a claim about the past (AHC-0117, F-085)."""

    done: tuple[str, ...] = ()
    """The operations whose effects landed, as `operation:record`. Not what was
    attempted and not what was claimed: what the far system confirmed."""

    awaiting: tuple[str, ...] = ()
    """What somebody else is holding — `approval:apr_…`, `escalation:E-…`. The
    thing a person picking this up most needs to know, and the thing a transcript
    states least reliably, because the sentence that mentions it is the one most
    likely to have been compacted away."""

    def asking(self, text: str, concerns: tuple[str, ...] = ()) -> Facts:
        return self.model_copy(update={"asked": text.strip(), "concerns": concerns})

    def touching(self, record: str) -> Facts:
        if not record or record in self.records:
            return self
        return self.model_copy(update={"records": tuple(sorted((*self.records, record)))})

    def reading(self, tool: str, record: str) -> Facts:
        """A read that answered, kept as the tool and the row it read, so a later
        turn can read the same row the same way (AHC-0117). Bounded like the
        rest: a row read nine times is one entry."""
        entry = f"{tool}:{record}"
        if not tool or not record or entry in self.read:
            return self
        return self.model_copy(update={"read": tuple(sorted((*self.read, entry)))})

    def landed(self, operation: str, record: str) -> Facts:
        entry = f"{operation}:{record}" if record else operation
        if entry in self.done:
            return self
        return self.model_copy(update={"done": (*self.done, entry)})

    def waiting_on(self, kind: str, identifier: str) -> Facts:
        entry = f"{kind}:{identifier}"
        if entry in self.awaiting:
            return self
        return self.model_copy(update={"awaiting": (*self.awaiting, entry)})

    def settled(self, kind: str, identifier: str) -> Facts:
        """Whatever was being waited on has been answered."""
        entry = f"{kind}:{identifier}"
        return self.model_copy(update={"awaiting": tuple(a for a in self.awaiting if a != entry)})

    def as_handoff(self) -> str:
        """What a person is handed, in the order they need it.

        Assembled rather than summarised, so nothing here is a paraphrase and
        nothing can be lost to a reduction. It is short because the record is
        bounded, which is the whole reason for keeping it this shape.
        """
        lines = [f"They asked: {self.asked}" if self.asked else "Nothing asked yet."]
        if len(self.concerns) > 1:
            lines.append(f"They raised {len(self.concerns)} things — each needs an outcome:")
            lines += [f"  {n}. {c}" for n, c in enumerate(self.concerns, 1)]
        if self.records:
            lines.append(f"About: {', '.join(self.records)}")
        lines.append(f"Already done: {', '.join(self.done)}" if self.done else "Nothing done yet.")
        if self.awaiting:
            lines.append(f"Waiting on: {', '.join(self.awaiting)}")
        return "\n".join(lines)


def after(
    facts: Facts,
    result: TurnResult,
    landed: tuple[tuple[str, str], ...] = (),
    looked: tuple[tuple[str, str], ...] = (),
) -> Facts:
    """What the turn's outcome adds to the record (AHC-0108).

    Two kinds of thing. What the far system confirmed — which is the fact half
    of the fact-versus-claim distinction, and arrives here from the tool
    boundary because that is the only place the difference between an action
    taken and an action described exists. And what somebody else is now holding,
    which is what the transcript states least reliably, because the sentence
    mentioning it is the one most likely to have been compacted away.
    """
    for operation, record in landed:
        facts = facts.touching(record).landed(operation, record)
    for tool, record in looked:
        facts = facts.reading(tool, record)
    match result:
        case NeedsApproval():
            return facts.waiting_on("approval", result.approval_id)
        case Escalated():
            return facts.waiting_on("escalation", result.ticket_id)
        case _:
            return facts


__all__ = ["Facts", "after"]
