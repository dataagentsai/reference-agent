"""The online evaluation, by this shop's rules.

The watch — reading turns back, running rules, inferring outcomes, writing every
verdict — is the harness's (`agent_harness.watch`). What it checks here is this
shop's: the rules (`rules`), the thresholds they read and the obligations each
verdict evidences (`evidence`). `Watch` below is the harness's with those as its
defaults, so a watch built with only a source and a sink runs this shop's rules.

    uv run python scripts/watch.py --every 60
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_harness import watch as _watch
from agent_harness.watch import Report, Sink, Source, rules_version
from agent_harness.watch.engine import ConversationRule, Rule
from agent_harness.watch.verdicts import Evidence
from support_agent.watch.checks import Thresholds
from support_agent.watch.evidence import EVIDENCE
from support_agent.watch.rules import CONVERSATION_RULES, RULES

RULES_VERSION = rules_version(RULES)
"""Names the rule set a score was made by (AHC-0028)."""


@dataclass
class Watch(_watch.Watch):
    rules: tuple[Rule, ...] = RULES
    thresholds: Thresholds = field(default_factory=Thresholds)
    conversation_rules: tuple[ConversationRule, ...] = CONVERSATION_RULES
    evidence: dict[str, Evidence] = field(default_factory=lambda: EVIDENCE)


__all__ = ["RULES_VERSION", "Report", "Sink", "Source", "Watch", "rules_version"]
