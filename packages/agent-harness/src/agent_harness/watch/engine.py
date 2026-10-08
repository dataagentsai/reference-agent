"""The watch's judging: rules over turns and conversations, and the verdicts they give.

What an agent checks — its rules, the thresholds they read, and which
obligations each verdict evidences — is its own (the reference agent's is
`support_agent.watch.rules`). How a rule is run is not: a rule that needs the
words gives no verdict on a turn without them, synthetic turns are the canary's,
a conversation rule's verdict lands on the conversation's last turn, and every
verdict is written, the passes too.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_harness.watch.record import Turn
from agent_harness.watch.verdicts import Evidence, Finding, Severity, Verdict


@dataclass(frozen=True)
class Rule:
    id: str
    version: str
    pattern: str
    title: str
    severity: Severity
    needs_words: bool
    check: Callable[[Turn, Any], str | None]


@dataclass(frozen=True)
class ConversationRule:
    id: str
    version: str
    pattern: str
    title: str
    severity: Severity
    check: Callable[[Sequence[Turn], Any], str | None]


def evaluate(
    turns: Iterable[Turn],
    rules: Iterable[Rule],
    thresholds: Any,
    conversation_rules: Iterable[ConversationRule] = (),
    evidence: Mapping[str, Evidence] | None = None,
) -> tuple[list[Finding], list[Turn]]:
    """Every finding in `turns`, and the turns evaluated."""
    verdicts, evaluated = judge(turns, rules, thresholds, conversation_rules, evidence)
    return [v.finding() for v in verdicts if not v.passed], evaluated


def judge(
    turns: Iterable[Turn],
    rules: Iterable[Rule],
    thresholds: Any,
    conversation_rules: Iterable[ConversationRule] = (),
    evidence: Mapping[str, Evidence] | None = None,
) -> tuple[list[Verdict], list[Turn]]:
    """Every rule's verdict on `turns`, passes included, and the turns evaluated.
    Synthetic turns are the canary's and are left to it (AHC-0113).

    A rule that needs the words gives no verdict on a turn without them — not a
    pass, and not a `skipped` score either: nothing is written, so the report
    reads that obligation from the turns that were captured. A conversation
    rule reads the captured turns of each conversation in the batch and its
    verdict lands on the last of them.
    """
    limits = thresholds
    table = evidence or {}
    evaluated = [t for t in turns if not t.synthetic]
    verdicts = [v for turn in evaluated for v in _of_turn(turn, tuple(rules), limits, table)]
    sessions: dict[str, list[Turn]] = defaultdict(list)
    for turn in (t for t in evaluated if t.captured):
        sessions[turn.session_id].append(turn)
    for session in sessions.values():
        verdicts += _of_conversation(
            sorted(session, key=lambda t: t.started), conversation_rules, limits, table
        )
    return verdicts, evaluated


def _of_turn(
    turn: Turn, rules: tuple[Rule, ...], limits: Any, table: Mapping[str, Evidence]
) -> list[Verdict]:
    return [
        _verdict(rule, turn, rule.check(turn, limits), table)
        for rule in rules
        if turn.captured or not rule.needs_words
    ]


def _of_conversation(
    ordered: list[Turn],
    rules: Iterable[ConversationRule],
    limits: Any,
    table: Mapping[str, Evidence],
) -> list[Verdict]:
    return [_verdict(rule, ordered[-1], rule.check(ordered, limits), table) for rule in rules]


def _verdict(
    rule: Rule | ConversationRule, turn: Turn, detail: str | None, table: Mapping[str, Evidence]
) -> Verdict:
    evidence = table.get(rule.id, Evidence((), "M5"))
    return Verdict(
        rule.id,
        rule.version,
        rule.severity,
        turn.trace_id,
        turn.session_id,
        detail,
        evidence.aac,
        evidence.mechanism,
    )


__all__ = ["ConversationRule", "Rule", "evaluate", "judge"]
