"""The `rule` kind: our own deterministic checks, as evaluators.

An agent's reply rules (`policy.Rule`, a function of a `policy.Context`) and the
library's golden-case checks (a function of an `EvalRequest`) both become
`Evaluator`s here. A rule is fast, free and ours: speed `inline`, provider
`ours`, cost 0, score 1 or 0.

What a rule needs is declared beside it in the agent's catalogue (`RuleSpec`),
not guessed: a grounding rule needs the tool results, because run with none it
would block "your refund is on its way" on a route that only says it after the
refund succeeded.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from agent_harness.contracts import Identity
from agent_harness.evals import EvalRequest, EvalResult, Meta, Outcome, Response, Speed, injection
from agent_harness.policy import Context, Position, Rule, Verdict
from agent_harness.policy.verdicts import ALLOW, block

RequestRule = Callable[[EvalRequest], Verdict]
"""A check that reads the whole request — a golden case's expectations — rather
than the reply's `policy.Context`."""

INLINE_MS = 5.0
"""A rule's declared cost class: a regular expression or a set comparison."""


@dataclass(frozen=True)
class RuleSpec:
    """One entry in an agent's rule catalogue: the check, what it needs, and its version."""

    check: Rule | RequestRule
    needs: frozenset[str] = frozenset({"response"})
    version: str = "1"
    reads_request: bool = False
    """True when `check` takes the `EvalRequest` itself (a golden-case check)."""


def context_of(request: EvalRequest) -> Context:
    """The `policy.Context` a reply rule reads, rebuilt from the request.

    After the model when the tool results were recorded; on any route's reply
    otherwise. Rules at these positions read only the text and the results.
    """
    response = request.response or Response(text="")
    results = response.tool_results
    return Context(
        position=Position.REPLY if results is None else Position.POST_MODEL,
        identity=Identity(customer_id=request.meta.user),
        text=response.text,
        tool_results=results or (),
    )


def request_of(ctx: Context, *, position: str = "reply") -> EvalRequest:
    """The request for a reply rule's `Context`: what the inline position runs on.
    The tool results are present after the model and absent on any route's reply."""
    results = ctx.tool_results if ctx.position is Position.POST_MODEL else None
    return EvalRequest(
        response=Response(text=ctx.text, tool_results=results),
        meta=Meta(position=position, user=ctx.identity.customer_id),
    )


@dataclass(frozen=True)
class RuleEvaluator:
    """An `Evaluator` over one catalogued rule."""

    name: str
    spec: RuleSpec
    threshold: float = 1.0
    provider: str = "ours"
    speed: Speed = "inline"
    expected_ms: float = INLINE_MS

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def needs(self) -> frozenset[str]:
        return self.spec.needs

    def evaluate(self, request: EvalRequest) -> EvalResult:
        started = time.perf_counter()
        try:
            if self.spec.reads_request:
                verdict = cast(RequestRule, self.spec.check)(request)
            else:
                verdict = cast(Rule, self.spec.check)(context_of(request))
        except Exception as exc:  # noqa: BLE001 — reported as `error`; inline fails closed
            return self._result("error", None, str(exc), self.name, started)
        score = 1.0 if verdict.allowed else 0.0
        outcome: Outcome = "pass" if score >= self.threshold else "fail"
        return self._result(outcome, score, verdict.reason, verdict.rule or self.name, started)

    def _result(
        self, outcome: Outcome, score: float | None, reason: str, label: str, started: float
    ) -> EvalResult:
        return EvalResult(
            verdict=outcome,
            score=score,
            raw_score=score,
            threshold=self.threshold,
            reason=reason,
            evaluator=self.name,
            version=self.version,
            provider=self.provider,
            latency_ms=(time.perf_counter() - started) * 1000,
            label=label,
        )


class RuleKind:
    """The kind adapter the registry loads for `kind: rule`."""

    fields = frozenset({"rule", "threshold", "version"})
    """`rule`: the catalogue entry, when it is not the evaluator's own name."""

    @staticmethod
    def build(
        name: str, spec: Mapping[str, Any], catalogue: Mapping[str, RuleSpec]
    ) -> RuleEvaluator:
        wanted = str(spec.get("rule", name))
        if wanted not in catalogue:
            known = ", ".join(sorted(catalogue))
            raise KeyError(
                f"evaluator {name!r}: no rule {wanted!r} in the catalogue; known: {known}"
            )
        entry = catalogue[wanted]
        if "version" in spec:
            entry = RuleSpec(entry.check, entry.needs, str(spec["version"]), entry.reads_request)
        return RuleEvaluator(name=name, spec=entry, threshold=float(spec.get("threshold", 1.0)))


# --------------------------------------------------------------------------- #
# The library's own rules: golden-case checks every agent can place.
# --------------------------------------------------------------------------- #


def _called(request: EvalRequest) -> set[str]:
    calls = request.response.tool_calls if request.response else None
    return {c.name for c in calls or ()}


def tool_selection(request: EvalRequest) -> Verdict:
    """The tools a golden case says must be called were, and the ones it forbids were not."""
    expected = request.expected
    if expected is None:
        return ALLOW
    called = _called(request)
    missing = sorted(set(expected.must_call) - called)
    if missing:
        return block("tool_selection", f"did not call {', '.join(missing)}")
    forbidden = sorted(set(expected.must_not_call) & called)
    if forbidden:
        return block("tool_selection", f"called {', '.join(forbidden)}, which it must not")
    return ALLOW


def must_include(request: EvalRequest) -> Verdict:
    """Every phrase a golden case says the reply must hold is in it (case-blind)."""
    if request.expected is None or request.response is None:
        return ALLOW
    text = request.response.text.lower()
    absent = [p for p in request.expected.must_include if p.lower() not in text]
    if absent:
        return block("must_include", f"the reply does not say {absent[0]!r}")
    return ALLOW


LIBRARY_RULES: dict[str, RuleSpec] = {
    "tool_selection": RuleSpec(
        tool_selection, frozenset({"tool_calls", "expected"}), reads_request=True
    ),
    "must_include": RuleSpec(must_include, frozenset({"response", "expected"}), reads_request=True),
    # A11: known injection phrasing (`evals.injection`), placed at pre_model and post_tool.
    "no_known_injection": RuleSpec(
        injection.no_known_injection, frozenset({"query"}), injection.VERSION, reads_request=True
    ),
    "no_instructions_in_result": RuleSpec(
        injection.no_instructions_in_result,
        frozenset({"tool_results"}),
        injection.VERSION,
        reads_request=True,
    ),
}
"""Placed by any agent's `evaluators.yaml` beside its own rules."""


__all__ = [
    "INLINE_MS",
    "LIBRARY_RULES",
    "RequestRule",
    "RuleEvaluator",
    "RuleKind",
    "RuleSpec",
    "context_of",
    "must_include",
    "request_of",
    "tool_selection",
]
