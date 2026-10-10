"""Checks as plug-ins: one request in, one result out, every check placed by YAML.

L7 and L12 (the architecture deck, slides 93–94). Every evaluator — our reply
rules, a guardrail's logged verdict, Presidio, an Azure built-in, an open judge
model — sits behind one port, `Evaluator`, and is handed one shape,
`EvalRequest`, and answers in one shape, `EvalResult`. A provider's own shape
stays inside its adapter: our rules never see Azure's data mapping and Azure
never sees a `policy.Context`.

Where each one runs is configuration (`evaluators.yaml`, read by `evals.plan`):

    reply     inline, before the customer reads it; a fail blocks (safe reply)
    online    after the turn, sampled; a fail is a finding or an alert
    release   over golden cases and scenarios; a fail blocks the release

**Each evaluator declares what it needs.** A request that lacks it is
`skip: missing <input>`, never a failure, so a turn with no retrieval is not
failed by a groundedness check. And a position that can never provide it
refuses the evaluator at startup, so a tool-selection check cannot be placed
where there are no tool definitions.

No agent and no vendor SDK is imported here or in any module beneath it (an
import contract holds that line); a vendor's evaluators are its own adapter
module, registered by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from agent_harness.contracts import ToolCall, ToolResult

Outcome = Literal["pass", "fail", "skip", "error"]
"""An evaluator's verdict. `skip`: it could not judge (an input was missing);
`error`: it tried and broke, which inline fails closed (AAC-0091)."""

Speed = Literal["inline", "online", "offline"]
"""How long an evaluator may take, as a class: `inline` fits inside a
customer's wait, `online` runs after the turn, `offline` only in a batch."""

NEEDS = (
    "query",
    "messages",
    "response",
    "tool_calls",
    "tool_results",
    "tool_definitions",
    "context",
    "expected",
)
"""Every input an evaluator may declare it needs: the request's fields, with
the response's tool calls and results named apart because a reply can be
judged on its text where its tools were not recorded."""


@dataclass(frozen=True)
class Response:
    """The reply, with its tool calls and their results. `None` for calls or
    results means *not recorded*; an empty tuple means *none were made*."""

    text: str
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_results: tuple[ToolResult, ...] | None = None
    gateway: tuple[Mapping[str, str], ...] = ()
    """What a gateway said about each model call behind this reply (APIM's
    `x-content-safety`, `llm.gateway`): read by `guardrail_log`; empty where
    there was no gateway or it was not recorded."""


@dataclass(frozen=True)
class Passage:
    """One retrieved passage, with the id of the source it came from."""

    source_id: str
    text: str


@dataclass(frozen=True)
class Expected:
    """What a golden case says should happen. Only a golden case has one."""

    route: str | None = None
    must_call: tuple[str, ...] = ()
    must_not_call: tuple[str, ...] = ()
    must_include: tuple[str, ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    """The expected actions, as `{name, arguments}`: what an action-matching
    evaluator compares the tool calls with."""


@dataclass(frozen=True)
class Meta:
    agent: str = ""
    version: str = ""
    trace: str = ""
    position: str = ""
    """`reply`, `online` or `release`: where this request is being judged."""
    language: str = "en"
    user: str = ""
    """Whose turn it was (the customer id). Never sent to a vendor evaluator."""


@dataclass(frozen=True)
class EvalRequest:
    """What every evaluator is handed. Conversation and tools in OpenAI's
    message and tool formats, so a vendor adapter maps fields, not shapes."""

    query: str | None = None
    messages: tuple[Mapping[str, Any], ...] | None = None
    response: Response | None = None
    tool_definitions: tuple[Mapping[str, Any], ...] | None = None
    """The tools this customer was allowed, as OpenAI function definitions."""
    context: tuple[Passage, ...] | None = None
    expected: Expected | None = None
    meta: Meta = field(default_factory=Meta)

    def provides(self) -> frozenset[str]:
        """The inputs present on this request, in `NEEDS`' words."""
        response = self.response
        present = {
            "query": self.query is not None,
            "messages": self.messages is not None,
            "response": response is not None,
            "tool_calls": response is not None and response.tool_calls is not None,
            "tool_results": response is not None and response.tool_results is not None,
            "tool_definitions": self.tool_definitions is not None,
            "context": self.context is not None,
            "expected": self.expected is not None,
        }
        return frozenset(name for name, here in present.items() if here)


@dataclass(frozen=True)
class EvalResult:
    """What every evaluator answers, whichever provider it is."""

    verdict: Outcome
    score: float | None
    """Normalised to 0–1, 1 best. `None` when skipped or errored."""
    raw_score: float | None
    """The provider's own number (a 1–5 rating, a 0/1), before normalising."""
    threshold: float | None
    """What `score` was judged against: pass when `score >= threshold`."""
    reason: str
    evaluator: str
    """The configured name: which plug-in this is in `evaluators.yaml`."""
    version: str
    provider: str
    """`ours`, `azure`, an open model's name: whose judgement this is."""
    cost: float = 0.0
    """What the call cost, in USD (tokens or a fee); feeds the cost engine."""
    latency_ms: float = 0.0
    label: str = ""
    """The provider's own label for the outcome (Azure returns one beside its
    score); for a rule, the id its verdict names."""

    @property
    def failed(self) -> bool:
        return self.verdict == "fail"


@runtime_checkable
class Evaluator(Protocol):
    """The port. One adapter per provider; one instance per configured name."""

    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def needs(self) -> frozenset[str]: ...
    @property
    def speed(self) -> Speed: ...
    @property
    def expected_ms(self) -> float:
        """The declared cost class in time: what an inline position's `max_ms`
        is compared with at startup."""
        ...

    def evaluate(self, request: EvalRequest) -> EvalResult: ...


def skipped(evaluator: Evaluator, missing: frozenset[str]) -> EvalResult:
    """The result for a request that lacks what `evaluator` needs."""
    return EvalResult(
        verdict="skip",
        score=None,
        raw_score=None,
        threshold=None,
        reason=f"skip: missing {', '.join(sorted(missing))}",
        evaluator=evaluator.name,
        version=evaluator.version,
        provider=evaluator.provider,
    )


def judge(evaluator: Evaluator, request: EvalRequest) -> EvalResult:
    """Run one evaluator, or skip it when the request lacks what it needs."""
    missing = evaluator.needs - request.provides()
    if missing:
        return skipped(evaluator, frozenset(missing))
    return evaluator.evaluate(request)


__all__ = [
    "NEEDS",
    "EvalRequest",
    "EvalResult",
    "Evaluator",
    "Expected",
    "Meta",
    "Outcome",
    "Passage",
    "Response",
    "Speed",
    "judge",
    "skipped",
]
