"""The `guardrail_log` kind: a guardrail's own verdict, recorded as a check result (A9).

APIM screens every prompt and completion with Content Safety and blocks what
it flags, before the agent sees it. This evaluator does not judge anything
again: it reads the verdict the gateway already gave, from the reply's model
calls (`Response.gateway`, the `x-content-safety` header, `llm.gateway`), so a
block, a pass and an outage are counted beside every other check's results.

    pass            every screened call passed
    fail            a call was blocked: the category is the label
                    (`block:Hate` -> `Hate`); `categories`, when given, limits
                    which categories fail (others pass, still labelled)
    skip            no call carried a verdict ("no gateway verdict": locally,
                    where there is no APIM), or Content Safety was unavailable
                    and the gateway failed open (label `unavailable`, F-40)

It never blocks: the gateway already did. So it belongs where a fail is a
record (`online`), and a placement where a fail would refuse a reply is the
YAML's mistake to make, not this kind's.

The header's grammar is the policy's (`infra/policies/groq-api.xml` in
claims-fnol-azure): `<overall>; prompt=<v>; completion=<v>`, each `<v>` one of
`pass`, `block:<category>`, `unavailable`, `skipped`. A header without the
overall part is read from its parts.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_harness.evals import EvalRequest, EvalResult, Outcome, Speed

SOURCE = "x-content-safety"
"""The signal the verdict arrives under, unless the YAML names another."""


def verdict_of(value: str) -> str:
    """The overall verdict in one header value: `pass`, `block:<category>`,
    `unavailable` or `skipped`."""
    parts = [p.strip() for p in value.split(";") if p.strip()]
    overall = [p for p in parts if "=" not in p]
    if overall:
        return overall[0]
    named = [p.partition("=")[2].strip() for p in parts]
    if blocked := [v for v in named if v.startswith("block")]:
        return blocked[0]
    if "unavailable" in named:
        return "unavailable"
    return "pass" if "pass" in named else "skipped"


@dataclass(frozen=True)
class GuardrailLog:
    """An `Evaluator` over the gateway's recorded verdicts."""

    name: str
    source: str = SOURCE
    categories: frozenset[str] = frozenset()
    """The categories whose block fails; empty means every one."""
    version: str = "1"
    provider: str = "gateway"
    needs: frozenset[str] = frozenset({"response"})
    speed: Speed = "inline"
    expected_ms: float = 1.0

    def evaluate(self, request: EvalRequest) -> EvalResult:
        started = time.perf_counter()
        calls = request.response.gateway if request.response else ()
        verdicts = [verdict_of(c[self.source]) for c in calls if self.source in c]
        screened = [v for v in verdicts if v != "skipped"]
        blocked = [v.partition(":")[2] or "unspecified" for v in screened if v.startswith("block")]
        failing = [c for c in blocked if not self.categories or c in self.categories]
        if failing:
            return self._result(
                started, "fail", 0.0, f"the gateway blocked: {failing[0]}", failing[0]
            )
        if blocked:
            reason = f"blocked for {blocked[0]}, not a category this check judges"
            return self._result(started, "pass", 1.0, reason, blocked[0])
        if "unavailable" in screened:
            reason = "Content Safety was unavailable; the gateway failed open"
            return self._result(started, "skip", None, reason, "unavailable")
        if not screened:
            return self._result(started, "skip", None, "skip: no gateway verdict", "")
        return self._result(started, "pass", 1.0, "the gateway passed every screened call", "pass")

    def _result(
        self, started: float, outcome: Outcome, score: float | None, reason: str, label: str
    ) -> EvalResult:
        took = (time.perf_counter() - started) * 1000
        return EvalResult(
            verdict=outcome,
            score=score,
            raw_score=score,
            threshold=None if score is None else 1.0,
            reason=reason,
            evaluator=self.name,
            version=self.version,
            provider=self.provider,
            latency_ms=took,
            label=label,
        )


class GuardrailLogKind:
    """The kind adapter the registry loads for `kind: guardrail_log`."""

    fields = frozenset({"source", "categories", "version"})

    @staticmethod
    def build(name: str, spec: Mapping[str, Any], catalogue: Mapping[str, Any]) -> GuardrailLog:
        categories = spec.get("categories") or ()
        if isinstance(categories, str) or not isinstance(categories, Iterable):
            raise ValueError(f"evaluator {name!r}: categories is a list, got {categories!r}")
        return GuardrailLog(
            name=name,
            source=str(spec.get("source", SOURCE)).lower(),
            categories=frozenset(str(c) for c in categories),
            version=str(spec.get("version", "1")),
        )


__all__ = ["SOURCE", "GuardrailLog", "GuardrailLogKind", "verdict_of"]
