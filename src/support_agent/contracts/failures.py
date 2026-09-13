"""AHC-0110 — one vocabulary for what kind of failure this is.

Fourteen exception types across ten modules, each named well for where it lives,
and no two of them named the same way. Every one is clear on its own and the set
is unusable from above: a caller's handler becomes a chain of `isinstance` checks
against a list somebody assembled by reading the code, and it is wrong the moment
a module adds a fifteenth — silently, because the new one falls through to
whatever the last branch does, which is always the most defensive thing to hand.

## Classified by what to do, not by where it came from

The origin is already in the record: the component raised it, the span names it,
the traceback holds it. A taxonomy repeating that adds nothing. The one question
a caller actually has is **is trying again sensible**, and origin cannot answer
it — `ToolUnavailable` and a tool refusing an argument are both "the tool", and
they need opposite responses.

So the axis is the response, and there are five kinds. A failure that fits none
of them is usually a failure whose handling nobody has decided yet, which is
worth finding out at the moment it is written rather than the first time it
happens.

## Closed, and checked

Widening this is a deliberate change with consumers to notify, which is the
right weight for something every caller branches on. And a test enumerates the
package's own failures and fails on one that declares no kind — because the rules
here that were stated and not checked are the ones that turned out to be quietly
untrue a quarter later.
"""

from __future__ import annotations

from enum import StrEnum


class Fault(StrEnum):
    """What a caller can do about it. Five kinds, and no sixth without a reason."""

    UNREACHABLE = "unreachable"
    """It could not be reached. Waiting and trying again is sensible, and this is
    the only kind for which it is: a provider that timed out, a tool server that
    is not answering, a store that dropped the connection."""

    REFUSED = "refused"
    """It was reached and said no. Trying again produces the same answer and
    costs the same money — an expired approval, a precondition that does not
    hold, an argument the far end will not accept. The distinction from
    `UNREACHABLE` is the whole reason this vocabulary exists."""

    MALFORMED = "malformed"
    """It answered, and the answer could not be read. Retrying is a coin flip
    that is usually paid for, so it is not the default: the same prompt to the
    same model mostly produces the same unreadable thing."""

    MISCONFIGURED = "misconfigured"
    """This build is wrong and no request will work — an unpriced model, a tool
    the registry does not know, a recording that does not match its request. The
    kind that should fail at startup and, where it does, never reaches a
    customer."""

    EXHAUSTED = "exhausted"
    """A declared bound was reached: steps, spend, a cassette with nothing left.
    Not an error in the system so much as the system doing what it was told, and
    a caller that retries it without raising the bound will exhaust it again."""


class AgentFailure(Exception):
    """Base for everything this package raises, carrying its kind.

    A class attribute rather than a constructor argument: the kind is a property
    of *what went wrong*, not of one occurrence of it, and making it per-instance
    would invite two call sites disagreeing about the same failure.
    """

    fault: Fault = Fault.REFUSED

    @property
    def kind(self) -> str:
        """For a span attribute or a metric label, where an enum is not wanted."""
        return self.fault.value


__all__ = ["AgentFailure", "Fault"]
