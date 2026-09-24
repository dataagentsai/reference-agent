"""One name, one outcome — the seam, and what every implementation speaks.

Declared here with the other ports because that is where a substitutable
boundary is declared: `requests` holds the realisations and `once`, and both
depend on this rather than the other way round.

The rule is one sentence, and `Scope` is where its one judgement lives:

    A name is owed until there is a definite answer. A definite answer is
    stored, and returned to anyone who repeats the name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from support_agent.contracts.failures import AgentFailure, Fault

CLAIM_TTL_S = 10 * 60
"""How long a claim may be held before it is treated as abandoned.

Long enough for the slowest turn a budget allows, short enough that a killed
process does not silence a customer's message until somebody notices. A claim
that never expired would be worse than no claim at all."""


class Scope(StrEnum):
    """What kind of name this is — and, with it, what *definite* means.

    One rule, and a scope is where it is decided whether reaching the end of the
    work counts as an answer. Declared here, once, rather than left to each
    caller to remember: two callers remembering differently is how this became
    two stores with opposite rules in the first place.
    """

    DELIVERY = "delivery"
    """A whole turn. Reaching the end **is** the answer, even when the turn
    raised: it ran, and re-running it would repeat whatever effects it managed
    before failing — the tool names that protected those effects carry a run id
    that a second run does not share. A process killed outright never reaches
    the end, and its claim is released by the expiry instead."""

    TOOL = "tool"
    """One call. Reaching the end with nothing to say is **indefinite**: the
    call may have landed and had its reply lost, and only the far end can tell.
    So the name is owed, and the retry goes out under it."""

    @property
    def answered_by_arriving(self) -> bool:
        """Whether finishing the work, with nothing to report, is an answer."""
        return self is Scope.DELIVERY


class RequestRefused(AgentFailure):
    """This name must not run again now."""

    fault = Fault.REFUSED


class AlreadyAnswered(RequestRefused):
    """A definite answer exists, and this is it.

    Not an error. The caller did the right thing by repeating the name, and is
    handed the answer rather than told that one exists — which is the whole
    difference between *already handled* and *your order was cancelled*.
    """

    def __init__(self, message: str, *, outcome: dict[str, object] | None = None) -> None:
        self.outcome = outcome
        super().__init__(message)


class StillRunning(RequestRefused):
    """Somebody holds this claim and has not finished.

    Distinct from `AlreadyAnswered` on purpose: a repeat is routine and two
    holders at once is a race worth looking at. The operator response differs,
    so the type does.
    """


@dataclass
class Claim:
    """A held name. Not frozen, because `outcome` is written by the body that
    holds it — the claim is taken before the work and settled after it."""

    name: str
    scope: Scope
    outcome: dict[str, object] | None = None
