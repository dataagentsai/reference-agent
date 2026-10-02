"""The online rules: what is checked in every evaluated turn and conversation.

These are the rules no product ships, because each knows what *this* agent's
tools and statuses mean: that `get_order` returned `pending` and the reply said
`shipped`, or that the reply said *cancelled* and no cancellation succeeded.
Generic scoring — a judge for tone, a threshold on latency — is adopted
elsewhere (Langfuse's evaluators, Prometheus rules); this is the delta.

**Every rule implements a pattern.** The catalog of what can go wrong, and how it
shows in telemetry, is AAC's `patterns/` (AACP-xxxx, each with a `signal`); a
rule here is one pattern's turn- or conversation-level signal made executable,
the way a cost analyser's detector implements one anti-pattern. Window-level
signals — rates against a baseline — are Prometheus rules in
`deploy/prometheus/rules/agent.yml`, which cite their patterns the same way.
`tests/test_watch_catalog.py` holds the three together.

**Each rule has an id and a version**, so a finding is attributable to the rule
*and the version* that made it (AHC-0028).

**A rule that needs the words says so.** On a turn whose words were not
captured it does not run, and the turn is not counted as passing it: a
contradiction nobody could see is not an absence of contradictions.

**Severity is what happens next**, not how bad it sounds. `page` is certain and
means a person is looking at the wrong thing right now; `ticket` wants reading
this week; `trend` matters only as a rate, and Prometheus alerts on the rate.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from support_agent.watch import checks as c
from support_agent.watch.checks import CLAIMS, STATUSES, Thresholds
from support_agent.watch.evidence import (
    BEYOND_THE_PATTERN,
    EVIDENCE,
    Evidence,
    Finding,
    Severity,
    Verdict,
)
from support_agent.watch.record import Turn


@dataclass(frozen=True)
class Rule:
    id: str
    version: str
    pattern: str
    title: str
    severity: Severity
    needs_words: bool
    check: Callable[[Turn, Thresholds], str | None]


@dataclass(frozen=True)
class ConversationRule:
    id: str
    version: str
    pattern: str
    title: str
    severity: Severity
    check: Callable[[Sequence[Turn], Thresholds], str | None]


RULES: tuple[Rule, ...] = (
    Rule(
        "W-01",
        "1",
        "AACP-0028",
        "Reply contradicts what the store said",
        "page",
        True,
        c.contradicts_tool,
    ),
    Rule(
        "W-02",
        "1",
        "AACP-0029",
        "Reply claims an action that did not happen",
        "page",
        True,
        c.claims_undone_action,
    ),
    Rule(
        "W-03",
        "1",
        "AACP-0033",
        "Personal data in a reply",
        "page",
        False,
        c.personal_data_in_reply,
    ),
    Rule(
        "W-04", "1", "AACP-0036", "Run ended by a limit, not an answer", "ticket", False, c.gave_up
    ),
    Rule(
        "W-05",
        "1",
        "AACP-0039",
        "Answered over a failed tool",
        "ticket",
        False,
        c.answered_over_a_failed_tool,
    ),
    Rule(
        "W-06",
        "2",
        "AACP-0014",
        "The same tool failing and called again",
        "ticket",
        False,
        c.ping_pong,
    ),
    Rule("W-07", "1", "AACP-0051", "Slow turn", "trend", False, c.slow),
    Rule("W-08", "1", "AACP-0053", "Costly turn", "trend", False, c.costly),
    Rule(
        "W-09", "1", "AACP-0034", "Completed with an empty answer", "ticket", True, c.empty_answer
    ),
    Rule("W-10", "1", "AACP-0048", "Unbacked promise", "ticket", False, c.unbacked_promise),
    Rule(
        "W-11",
        "1",
        "AACP-0046",
        "Refused a question about an order",
        "trend",
        True,
        c.refused_an_order_question,
    ),
    Rule("W-12", "1", "AACP-0038", "Unreadable model output", "trend", False, c.malformed_model),
    Rule(
        "W-13",
        "1",
        "AACP-0019",
        "A write retried after the store refused it",
        "ticket",
        True,
        c.retried_after_refusal,
    ),
    Rule(
        "W-14",
        "1",
        "AACP-0021",
        "A write on an order nothing read",
        "ticket",
        True,
        c.write_without_read,
    ),
    Rule(
        "W-15",
        "1",
        "AACP-0022",
        "A tool called for an order nobody named",
        "ticket",
        True,
        c.invented_argument,
    ),
    Rule(
        "W-16",
        "1",
        "AACP-0030",
        "Reply names an order nothing returned",
        "ticket",
        True,
        c.identifier_nobody_gave,
    ),
    Rule("W-17", "1", "AACP-0031", "Internal text in a reply", "ticket", True, c.internal_text),
    Rule(
        "W-18",
        "1",
        "AACP-0032",
        "Asked for an order it was given",
        "ticket",
        True,
        c.asks_for_what_it_was_given,
    ),
    Rule(
        "W-19",
        "1",
        "AACP-0015",
        "A write after instructions arrived in a result",
        "page",
        True,
        c.write_after_injection,
    ),
    Rule(
        "W-20",
        "1",
        "AACP-0025",
        "Answered on a truncated result",
        "trend",
        False,
        c.answered_on_a_truncated_result,
    ),
    Rule(
        "W-21",
        "1",
        "AACP-0054",
        "Served by a model other than the one asked for",
        "ticket",
        False,
        c.served_another_model,
    ),
)

CONVERSATION_RULES: tuple[ConversationRule, ...] = (
    ConversationRule(
        "C-01", "1", "AACP-0040", "The customer repeats themselves", "trend", c.repeats_themselves
    ),
    ConversationRule(
        "C-02", "1", "AACP-0035", "Apologies without progress", "ticket", c.apology_without_progress
    ),
    ConversationRule(
        "C-03", "1", "AACP-0003", "Late turns cost far more than the first", "trend", c.tail_cost
    ),
)


NOT_YET: dict[str, str] = {
    "AACP-0043": (
        "nothing marks a conversation resolved: the watch infers that an answer did not"
        " hold (returned, asked_for_person) but never that one did, so turns per"
        " resolved conversation has no denominator; and a per-session label would be"
        " unbounded. Needs the AOAS to say what resolved means"
    ),
}
"""Patterns with a signal that nothing here detects yet, and why — so a gap is
a stated gap rather than a rule nobody wrote."""


def evaluate(
    turns: Iterable[Turn],
    rules: Iterable[Rule] = RULES,
    thresholds: Thresholds | None = None,
    conversation_rules: Iterable[ConversationRule] = CONVERSATION_RULES,
) -> tuple[list[Finding], list[Turn]]:
    """Every finding in `turns`, and the turns evaluated."""
    verdicts, evaluated = judge(turns, rules, thresholds, conversation_rules)
    return [v.finding() for v in verdicts if not v.passed], evaluated


def judge(
    turns: Iterable[Turn],
    rules: Iterable[Rule] = RULES,
    thresholds: Thresholds | None = None,
    conversation_rules: Iterable[ConversationRule] = CONVERSATION_RULES,
) -> tuple[list[Verdict], list[Turn]]:
    """Every rule's verdict on `turns`, passes included, and the turns evaluated.
    Synthetic turns are the canary's and are left to it (AHC-0113).

    A rule that needs the words gives no verdict on a turn without them — not a
    pass, and not a `skipped` score either: nothing is written, so the report
    reads that obligation from the turns that were captured. A conversation
    rule reads the captured turns of each conversation in the batch and its
    verdict lands on the last of them.
    """
    limits = thresholds or Thresholds()
    evaluated = [t for t in turns if not t.synthetic]
    verdicts = [v for turn in evaluated for v in _of_turn(turn, tuple(rules), limits)]
    sessions: dict[str, list[Turn]] = defaultdict(list)
    for turn in (t for t in evaluated if t.captured):
        sessions[turn.session_id].append(turn)
    for session in sessions.values():
        verdicts += _of_conversation(
            sorted(session, key=lambda t: t.started), conversation_rules, limits
        )
    return verdicts, evaluated


def _of_turn(turn: Turn, rules: tuple[Rule, ...], limits: Thresholds) -> list[Verdict]:
    return [
        _verdict(rule, turn, rule.check(turn, limits))
        for rule in rules
        if turn.captured or not rule.needs_words
    ]


def _of_conversation(
    ordered: list[Turn], rules: Iterable[ConversationRule], limits: Thresholds
) -> list[Verdict]:
    return [_verdict(rule, ordered[-1], rule.check(ordered, limits)) for rule in rules]


def _verdict(rule: Rule | ConversationRule, turn: Turn, detail: str | None) -> Verdict:
    evidence = EVIDENCE.get(rule.id, Evidence((), "M5"))
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


__all__ = [
    "BEYOND_THE_PATTERN",
    "CLAIMS",
    "CONVERSATION_RULES",
    "EVIDENCE",
    "NOT_YET",
    "RULES",
    "STATUSES",
    "ConversationRule",
    "Evidence",
    "Finding",
    "Rule",
    "Thresholds",
    "Verdict",
    "evaluate",
    "judge",
]
