"""AHC-0106 — a reply that commits to work nothing is doing.

The mirror of abstention. `AHC-0063` says *"I do not know"* must be a typed
outcome rather than a phrasing choice; this says the same of *"I will"*. A turn
that ends as `Completed` has, by the definition of that type, nothing that will
produce a later answer: no approval pending, no ticket open, no continuation the
caller can drive. So a `Completed` whose words promise one is a state the system
cannot honour, and the customer waits for something nobody is doing.

**The structural half is the result type, and it does the work.** Nothing here
reads whether a tool ran or a row moved, because `Completed` already encodes the
answer: `NeedsApproval` carries an approval id, `Escalated` carries a ticket, and
a turn that is neither has nothing behind it. A `Completed` that cancelled an
order and then promised a confirmation email is caught for the same reason as one
that did nothing at all — the email is not coming either.

**The wording half is a classifier, and it is the weak half.** It will miss a
phrasing nobody wrote down, and it would need this table again in every language
the agent answers in. It is kept narrow on purpose: only a first-person
commitment to a *named* future act. "I will do what I can" is an offer to
continue now and is deliberately not here — `CAPPED_REPLY` and `LAPSED_REPLY`
both end that way, and both are true.

The two halves are both necessary. The type alone over-fires: most completed
answers are pure text and perfectly honest. The wording alone is the classifier
running against every reply, including the ones a ticket already backs —
`RAISED_REPLY` opens with *"Let me pass you to a colleague"* and is the truest
sentence here, because the reference follows it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agent_harness import telemetry as tel
from support_agent.contracts import (
    Completed,
    Escalate,
    Escalated,
    Failed,
    Identity,
    RunId,
    TurnResult,
)

if TYPE_CHECKING:
    from agent_harness.state import Conversation
    from support_agent.entrypoint.handoff import Handoff

VERSION = "1"
"""Bumped when the table changes, so a run recorded against one reading of this
rule is not compared with a run against another."""

# One first-person commitment to a named future act. Each alternative is a verb
# somebody could be waiting on the outcome of — deliberately not "do", "try" or
# "help", which promise effort rather than an answer.
_ACTS = (
    r"check|look|see|find\s+out|confirm|verify|investigate|chase|sort|arrange|"
    r"raise|escalate|pass|transfer|hand|get|ask|contact|email|call|update|inform"
)

COMMITMENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "first-person future act",
        re.compile(
            rf"\b(?:let me|i'?ll|i will|i am going to|i'?m going to|we'?ll|we will)\s+"
            rf"(?:go\s+)?(?:and\s+)?(?:just\s+)?(?:{_ACTS})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "will return with an answer",
        re.compile(
            r"\b(?:get|come)\s+back\s+to\s+you\b|\b(?:i|we)\s+will\s+"
            r"(?:let\s+you\s+know|be\s+in\s+touch|keep\s+you\s+(?:posted|updated))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "asks the customer to wait",
        re.compile(
            r"\b(?:one moment|just a moment|bear with me|give me a "
            r"(?:moment|second|minute))\b",
            re.IGNORECASE,
        ),
    ),
)
"""Three shapes, each with the name that goes on the span. A name rather than a
pattern index, because the question worth asking later is *which kind of promise
does this agent make that it cannot keep* — and a number cannot be read."""


def commits(reply: str) -> str:
    """The name of the commitment this reply makes, or `""` if it makes none.

    Returns the name rather than a bool so what fired is recordable. A rule that
    fires invisibly is one nobody can tune, and this one is a classifier over
    open-ended text — the kind most in need of tuning.
    """
    for name, pattern in COMMITMENTS:
        if pattern.search(reply):
            return name
    return ""


UNBACKED_PROMISE = "unbacked-commitment"
"""The AOAS's `on_reply` rule an unbacked promise is escalated under (it was
an undeclared, refusal-shaped id until generation run 5), so the desk's records
group by it. The number worth watching is how often the model writes a cheque
this agent cannot cash."""

WITHDRAWN_REPLY = (
    "I have not been able to finish this, and I cannot hand it to a colleague from "
    "here. Tell me what you need and I will do what I can."
)
"""What is said when the promise cannot be made true and nobody can be fetched.

It must not itself commit — a replacement that promised something would be the
same defect with better manners — and a test asserts exactly that by running it
back through `commits`."""


async def honest(
    result: TurnResult,
    desk: Handoff,
    conversation: Conversation,
    identity: Identity,
    run_id: RunId,
) -> TurnResult:
    """Make the promise true, or withdraw it.

    Called **after** Tier 2, because that is the first point at which the
    question is answerable: until the desk has had its chance, a turn that looks
    like an unbacked promise may be one about to be handed to a person.
    Afterwards `Completed` means what its type says — nothing pending, nothing
    open, nothing that will produce what was just promised.

    The desk is asked for a handoff, which is the continuation this agent has.
    Where there is none, **the words are replaced and the result is not** — the
    turn keeps its type and its termination, so a run stopped by the cost ceiling
    is still recorded as stopped by the cost ceiling. An earlier version returned
    the desk's refusal instead and lost that: the trace said `refused`, and why
    the agent gave up was gone. Removing a false promise must not also remove the
    record of what happened.

    A held turn does not pass here. Its replies are the desk's own constants, and
    every one of them names the ticket that backs it.
    """
    if not isinstance(result, Completed):
        return result
    promise = commits(result.reply)
    if not promise:
        return result
    tel.counters.promises.add(1, {"kind": promise})
    with tel.span(
        "agent.promise.unbacked",
        **{"agent.promise.kind": promise, "agent.promise.rules_version": VERSION},
    ):
        handed = await desk.raise_requested(
            Escalate(
                reason="the agent said it would come back and nothing was doing so",
                rule_id=UNBACKED_PROMISE,
                tier=2,
            ),
            conversation,
            identity,
            run_id,
        )
    # Only a handoff makes the promise true, and only that replaces the result.
    # Every other answer the desk gives — no store to write to, or a conversation
    # already past its cap — leaves the turn exactly as it was apart from the
    # sentence nothing was going to honour. The desk's own words are used where
    # it gave any, because it knows more than this does about why it declined:
    # "somebody already has this" and "I cannot fetch anyone from here" are
    # different truths, and neither is `WITHDRAWN_REPLY`'s.
    if isinstance(handed, Escalated):
        return handed
    declined = handed.customer_message if isinstance(handed, Failed) else handed.reply
    return result.model_copy(update={"reply": declined or WITHDRAWN_REPLY})


__all__ = ["COMMITMENTS", "UNBACKED_PROMISE", "VERSION", "WITHDRAWN_REPLY", "commits", "honest"]
