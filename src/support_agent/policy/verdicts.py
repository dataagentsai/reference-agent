"""Where a rule runs, what it may look at, and what it returns.

Apart from the rules so a rule can live in a module of its own without importing
the package that lists it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from support_agent.contracts import Identity, ToolResult
from support_agent.policy.reading import normalised


class Position(StrEnum):
    """Where a rule runs. **Every one of these is reached** — a rule configured
    at any position runs at that position, which was not true until F-027: three
    of the five were declared, accepted rules, and called nothing.

    What a block *means* differs by position, and the difference is the design:
    before a model call or a reply, blocking ends the turn, because there is no
    lesser thing to do. Around a tool call it does not — the model is told and
    may choose again, which is what AAC-0051 asks for.
    """

    PRE_MODEL = "pre_model"
    """Before the request is sent. The last place to stop work that should not
    be paid for; the reply is the refusal."""
    POST_MODEL = "post_model"
    PRE_TOOL = "pre_tool"
    """Between the decision to call a tool and the call. Blocking returns an
    error result to the model rather than ending the turn — the action did not
    happen, and that is a fact the model can act on."""
    POST_TOOL = "post_tool"
    """After a result, before it enters context. Blocking replaces the result;
    it cannot un-happen the effect, and it does not pretend to."""
    REPLY = "reply"
    """What the customer is about to read, on every route — not only the one
    where the model wrote it (F-020)."""


@dataclass(frozen=True)
class Context:
    """What a rule may look at. Deliberately narrow — a rule that needs more than
    this is probably a business decision wearing a guardrail's coat."""

    position: Position
    identity: Identity
    text: str = ""
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    tool_results: tuple[ToolResult, ...] = ()
    """Everything the tools returned this turn: what a claim is checked against."""
    result: ToolResult | None = None
    """At `POST_TOOL`, the one result just returned, apart from the list above."""

    def __post_init__(self) -> None:  # every rule reads one spelling (AHC-0094)
        object.__setattr__(self, "text", normalised(self.text))


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: str = ""
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


ALLOW = Verdict(allowed=True)


def block(rule: str, reason: str) -> Verdict:
    return Verdict(allowed=False, rule=rule, reason=reason)


__all__ = ["ALLOW", "Context", "Position", "Verdict", "block"]
