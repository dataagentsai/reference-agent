"""What the customer is told about the desk — only what is now true."""

from __future__ import annotations

RAISED_REPLY = (
    "Let me pass you to a colleague. Your reference is {ticket}, and they will pick this up here."
)
"""Says only what is now true. The record exists and carries this id; nothing is
claimed about when a person will arrive, because nothing here knows."""


WAITING_REPLY = (
    "That is still with a colleague — your reference is {ticket}. I have not forgotten about it."
)


QUEUED_REPLY = (
    "Let me pass you to a colleague — your reference is {ticket}, and the wait is about {wait}."
)
"""Says a number, and only a number it can defend.

The estimate comes from queue depth divided by measured throughput. When there
is nothing to divide by — a desk whose rate we do not know — the agent falls
back to `RAISED_REPLY`, which promises a reference and no time at all. An
invented "just a few minutes" is the whole class of thing the system prompt
forbids."""


CLOSED_REPLY = (
    "The team is not available right now. I have logged this as {ticket} and "
    "they will pick it up when they are back."
)
"""Nobody is there. Saying so beats a reference number that implies somebody
is."""


NO_DESK_REPLY = (
    "I cannot pass this to a colleague from here. Tell me what you need and I will do what I can."
)
"""AOAS `escalate.on_refusal`. No desk is reachable, so there is nothing to hand
the conversation to — and "let me pass you to a colleague" would be a claimed
action with no record behind it (F-024). The offer is to stay with the request."""


LAPSED_REPLY = (
    "Nobody has picked up {ticket} yet, so I am back with you in the meantime. "
    "Tell me what you need and I will do what I can."
)
"""The honest version of a bad outcome. It does not pretend the escalation
succeeded, and it does not leave the customer with nothing."""


def humanise(seconds: int) -> str:
    """A wait a person can act on. Rounded, because false precision in a promise
    reads as a guarantee."""
    if seconds < 90:
        return "a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = round(minutes / 60)
    return "an hour" if hours == 1 else f"{hours} hours"


__all__ = [
    "CLOSED_REPLY",
    "NO_DESK_REPLY",
    "LAPSED_REPLY",
    "QUEUED_REPLY",
    "RAISED_REPLY",
    "WAITING_REPLY",
    "humanise",
]
