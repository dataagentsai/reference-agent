"""Handing a conversation to a person, in this shop's words and by its rules.

The handoff — holding a conversation a person owns, raising on request, raising
on a condition, the cap — is the harness's (`agent_harness.entrypoint.handoff`).
What it says is this shop's (`escalation.wording`, read through the
`support_agent.escalation` module at the moment it speaks, so the words are
whatever that module holds then), and which conditions earn a person is this
shop's Tier 2 rule set. Both are the defaults here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_harness.entrypoint import handoff as _handoff
from agent_harness.entrypoint.handoff import Handoff, HandoffWording
from agent_harness.escalation import rules as engine
from support_agent import escalation as esc
from support_agent.escalation import rules as t2


@dataclass(frozen=True)
class NoDesk(_handoff.NoDesk):
    wording: HandoffWording = field(default=esc)


@dataclass(frozen=True)
class HandoffDesk(_handoff.HandoffDesk):
    wording: HandoffWording = field(default=esc)
    tier_2: engine.RuleSet = field(default_factory=t2.RuleSet)


__all__ = ["Handoff", "HandoffDesk", "HandoffWording", "NoDesk"]
