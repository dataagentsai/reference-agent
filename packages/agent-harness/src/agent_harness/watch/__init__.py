"""The online evaluation: rules over production turns, and what happened next.

L12 · S5. AHC-0114's record read back, AAC-0014's scoring and AAC-0115's
outcomes, run on a schedule beside the agent rather than inside it — nothing
here is on a customer's request path, and nothing here can change a reply.

    uv run python scripts/watch.py --every 60

Each pass: read the turns that finished since the last pass (less a settling
delay, because the trace store ingests asynchronously), run the rules, infer
outcomes against the customer's last day, write every verdict to the trace store
as a score, and count everything as metrics. The counts are what alerts;
Prometheus holds the thresholds (T-055), so a threshold is a rule file anybody
can read rather than a constant in this package.

**What is adopted, what is the harness's, and what is an agent's.** The store,
the scores, the dashboards, the alerting and any model-graded rubric are
Langfuse's and Prometheus's. Running the rules, inferring outcomes and writing
every verdict is the harness's. What an agent's tools and statuses mean — its
rules, their thresholds and what each evidences — is the agent's, handed to
`Watch` (the reference agent's defaults are `support_agent.watch.Watch`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_harness import telemetry as tel
from agent_harness.watch import outcomes as oc
from agent_harness.watch import record
from agent_harness.watch.engine import ConversationRule, Rule, judge
from agent_harness.watch.outcomes import Outcome
from agent_harness.watch.record import Turn
from agent_harness.watch.verdicts import Evidence, Finding, Verdict


def rules_version(rules: Iterable[Rule]) -> str:
    """The rule set's version, derived from every rule's id and version.

    It was a hand-kept "1", said to be bumped whenever any rule's was, and W-06
    went to 2 without it (F-069): a score's version then named a rule set that
    no longer existed. Derived, it cannot be forgotten.
    """
    named = ",".join(sorted(f"{r.id}@{r.version}" for r in rules))
    return hashlib.sha256(named.encode()).hexdigest()[:12]


class Source(Protocol):
    def traces_between(self, start: float, end: float) -> list[Mapping[str, Any]]: ...
    def turns_of(self, user: str, start: float, end: float) -> list[Mapping[str, Any]]: ...
    def feedback_between(self, start: float, end: float) -> list[Mapping[str, Any]]: ...


class Sink(Protocol):
    def finding(self, found: Finding) -> None: ...
    def passed(self, verdict: Verdict) -> None: ...
    def evaluated(self, trace_id: str, findings: int, version: str) -> None: ...
    def outcome(self, found: Outcome) -> None: ...


@dataclass(frozen=True)
class Report:
    evaluated: tuple[Turn, ...]
    findings: tuple[Finding, ...]
    outcomes: tuple[Outcome, ...]


@dataclass
class Watch:
    source: Source
    sink: Sink
    rules: tuple[Rule, ...]
    """The agent's turn rules. Its version is derived from them (`rules_version`)."""
    thresholds: Any
    """The limits the rules read — the agent's type, passed through untouched."""
    conversation_rules: tuple[ConversationRule, ...] = ()
    evidence: Mapping[str, Evidence] = field(default_factory=dict)
    """Which obligations each rule's verdict evidences, by rule id."""
    settle_s: float = 60.0
    cursor: float = 0.0
    """The end of the last window evaluated. Starts at zero, which a caller
    replaces with *now minus a lookback* — a watch started cold does not score
    the whole history."""

    def once(self, now: float) -> Report:
        end = now - self.settle_s
        start = self.cursor or end - 3600
        if end <= start:
            return Report((), (), ())
        turns = record.turns(record.from_observations(self.source.traces_between(start, end)))
        verdicts, evaluated = judge(
            turns, self.rules, self.thresholds, self.conversation_rules, self.evidence
        )
        findings = [v.finding() for v in verdicts if not v.passed]
        found = [
            *oc.returned(evaluated, self._history(evaluated, end)),
            *oc.asked_for_person(evaluated),
            *self._stated(start, end),
        ]
        self._write(evaluated, verdicts, found)
        self.cursor = end
        return Report(tuple(evaluated), tuple(findings), tuple(found))

    def _history(self, turns: Iterable[Turn], end: float) -> list[Turn]:
        users = {t.user_id for t in turns}
        since = end - oc.RETURN_WINDOW_S - self.settle_s
        observed = [o for u in sorted(users) for o in self.source.turns_of(u, since, end)]
        return record.turns(record.from_observations(observed))

    def _stated(self, start: float, end: float) -> list[Outcome]:
        given = record.feedback(record.from_observations(self.source.feedback_between(start, end)))
        users = {f.user_id for f in given}
        observed = [
            o
            for u in sorted(users)
            for o in self.source.turns_of(u, start - oc.RETURN_WINDOW_S, end)
        ]
        return oc.stated(given, record.turns(record.from_observations(observed)))

    def _write(self, evaluated: list[Turn], verdicts: list[Verdict], found: list[Outcome]) -> None:
        per_trace: dict[str, int] = {t.trace_id: 0 for t in evaluated}
        version = rules_version(self.rules)  # the rules that ran, not the default set
        for verdict in verdicts:
            if verdict.passed:
                # Written as well, so the export says what held and not only
                # what fired (AAC's Langfuse adapter reads both).
                self.sink.passed(verdict)
                continue
            finding = verdict.finding()
            per_trace[finding.trace_id] += 1
            self.sink.finding(finding)
            tel.counters.findings.add(1, {"rule": finding.rule, "severity": finding.severity})
        for turn in evaluated:
            self.sink.evaluated(turn.trace_id, per_trace[turn.trace_id], version)
            tel.counters.scored.add(1, {"captured": "true" if turn.captured else "false"})
        for outcome in found:
            self.sink.outcome(outcome)
            tel.counters.outcomes.add(1, {"kind": outcome.kind, "source": outcome.source})


__all__ = ["Report", "Sink", "Source", "Watch", "rules_version"]
