"""`evaluators.yaml`, read into a typed plan that refuses to start when it is wrong.

    evaluators:                      # name -> kind, and the kind's own fields
      no_ungrounded_entity: {kind: rule}
      tool_selection: {kind: rule}
    positions:
      reply:                         # inline: a fail blocks, the customer gets the safe reply
        - {use: no_ungrounded_entity, on_fail: safe_reply, max_ms: 50}
      online:                        # after the turn, sampled
        sample: 0.05
        run:
          - {use: no_ungrounded_entity, sample: 1.0, on_fail: alert}
      release:                       # over golden cases: a pass rate under `min` blocks
        run:
          - {use: tool_selection, on_fail: block, min: 1.0}

Moving a check, changing a threshold or a sample rate is a change to this file
and nothing else. A new rule is code once (the agent's catalogue), then YAML; a
new kind of provider is one adapter module, registered in `KINDS`, then YAML.

**Startup refuses** an unknown kind, evaluator name or field; an evaluator at
`reply` that is not declared inline-fast within `max_ms`; and an evaluator whose
needs its position can never provide. A stub kind (`azure`, …) refuses only when
a YAML declares one.

`reply` reaches the harness's existing hook, `policy.use_default_rules`: its
evaluators become the `POST_MODEL` rules (the model's reply, with the turn's tool
results), and those that need nothing but the reply also run at `REPLY` (every
route's reply, where there are no tool results to read). Order is the YAML's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_harness.config.registry import NotBuilt, Registry, UnknownName
from agent_harness.contracts.failures import AgentFailure, Fault
from agent_harness.evals import NEEDS, EvalResult, Evaluator, judge
from agent_harness.evals.rule import LIBRARY_RULES, RuleSpec, request_of
from agent_harness.policy import ALLOW, Context, Rule, Verdict, block

KINDS = Registry(
    "evaluator kinds",
    {
        "kind": {
            "rule": "agent_harness.evals.rule:RuleKind",
            "guardrail_log": "agent_harness.evals.guardrail:GuardrailLogKind",
            "presidio": "agent_harness.evals.stubs:Presidio",
            "azure": "agent_harness.evals.stubs:AzureEvaluator",
            "open_model": "agent_harness.evals.stubs:OpenModel",
        }
    },
)
"""Each kind is an adapter, loaded only when a YAML names it."""

Position = Literal["reply", "online", "release"]
PROVIDES: dict[str, frozenset[str]] = {
    "reply": frozenset({"response", "tool_results"}),
    "online": frozenset(NEEDS) - {"expected"},
    "release": frozenset(NEEDS),
}
"""What each position can ever hand an evaluator. Only a golden case has `expected`."""
EVERY_ROUTE = frozenset({"response"})
"""What every route's reply provides: the text, and no tool results."""
ON_FAIL = {"reply": ("safe_reply", "alert"), "online": ("alert",), "release": ("block", "alert")}
ENTRY_FIELDS = {
    "reply": frozenset({"use", "on_fail", "max_ms"}),
    "online": frozenset({"use", "on_fail", "sample", "max_ms"}),
    "release": frozenset({"use", "on_fail", "min"}),
}
REPLY_MAX_MS = 50.0
"""An inline evaluator's budget when its entry names none: inside a customer's wait."""


class PlanRefused(AgentFailure):
    """`evaluators.yaml` is wrong; the process does not start."""

    fault = Fault.MISCONFIGURED


@dataclass(frozen=True)
class Placed:
    """One evaluator at one position, with what a fail there does."""

    evaluator: Evaluator
    on_fail: str
    sample: float = 1.0
    max_ms: float | None = None
    min: float = 1.0


@dataclass(frozen=True)
class Gate:
    """One release evaluator's pass rate over a batch, against its `min`."""

    evaluator: str
    passed: int
    failed: int
    skipped: int
    errored: int
    min: float
    on_fail: str

    @property
    def rate(self) -> float | None:
        judged = self.passed + self.failed
        return self.passed / judged if judged else None

    @property
    def blocks(self) -> bool:
        under = self.errored > 0 or (self.rate is not None and self.rate < self.min)
        return under and self.on_fail == "block"


@dataclass(frozen=True)
class Plan:
    evaluators: Mapping[str, Evaluator]
    reply: tuple[Placed, ...] = ()
    online: tuple[Placed, ...] = ()
    release: tuple[Placed, ...] = ()

    def inline(
        self,
        point: Literal["model", "every_route"],
        alert: Callable[[EvalResult], None] | None = None,
    ) -> tuple[Rule, ...]:
        """The `reply` evaluators as the harness's rules at one inline point."""
        provided = PROVIDES["reply"] if point == "model" else EVERY_ROUTE
        return tuple(_as_rule(p, alert) for p in self.reply if p.evaluator.needs <= provided)

    def run_online(self, request_for: Any, *, key: str) -> list[EvalResult]:
        """Every `online` evaluator this turn is sampled into. `request_for` is
        the turn's `EvalRequest`; `key` (its trace id) makes sampling repeatable."""
        return [
            judge(p.evaluator, request_for)
            for p in self.online
            if _sampled(f"{key}:{p.evaluator.name}", p.sample)
        ]

    def run_release(self, requests: Iterable[Any]) -> tuple[tuple[Gate, ...], list[EvalResult]]:
        """Every `release` evaluator over a batch: one gate each, and every result."""
        batch = list(requests)
        results: list[EvalResult] = []
        gates = []
        for placed in self.release:
            mine = [judge(placed.evaluator, r) for r in batch]
            results += mine
            count = {
                v: sum(r.verdict == v for r in mine) for v in ("pass", "fail", "skip", "error")
            }
            gates.append(
                Gate(
                    placed.evaluator.name,
                    count["pass"],
                    count["fail"],
                    count["skip"],
                    count["error"],
                    placed.min,
                    placed.on_fail,
                )
            )
        return tuple(gates), results

    def where(self, name: str) -> tuple[str, ...]:
        """The positions `name` is placed at."""
        held = {"reply": self.reply, "online": self.online, "release": self.release}
        return tuple(
            pos for pos, placed in held.items() if any(p.evaluator.name == name for p in placed)
        )


class EvaluatorFailed(AgentFailure):
    """An inline evaluator errored. Raised so `policy.enforce` fails closed and
    names it, exactly as a rule that raises (AAC-0091). Malformed: the check
    broke on this input, and the same input breaks it again."""

    fault = Fault.MALFORMED


def _as_rule(placed: Placed, alert: Callable[[EvalResult], None] | None) -> Rule:
    evaluator, on_fail = placed.evaluator, placed.on_fail

    def check(ctx: Context) -> Verdict:
        result = judge(evaluator, request_of(ctx))
        if result.verdict == "error":
            raise EvaluatorFailed(result.reason)
        if not result.failed:
            return ALLOW
        if on_fail == "safe_reply":
            return block(result.label or result.evaluator, result.reason)
        if alert is not None:
            alert(result)
        return ALLOW

    check.__name__ = check.__qualname__ = evaluator.name
    return check


def _sampled(key: str, rate: float) -> bool:
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


# --------------------------------------------------------------------------- #
# Reading and refusing.
# --------------------------------------------------------------------------- #


def load(
    source: Path | str,
    *,
    rules: Mapping[str, RuleSpec],
    kinds: Registry = KINDS,
) -> Plan:
    """The plan in `source` (a path, or the YAML itself), checked; `rules` is
    the agent's catalogue, beside the library's own."""
    text = source.read_text() if isinstance(source, Path) else source
    document = yaml.safe_load(text) or {}
    return parse(document, rules=rules, kinds=kinds)


def parse(
    document: Mapping[str, Any], *, rules: Mapping[str, RuleSpec], kinds: Registry = KINDS
) -> Plan:
    _only(document, frozenset({"evaluators", "positions"}), "evaluators.yaml")
    catalogue = {**LIBRARY_RULES, **rules}
    built = {
        name: _evaluator(name, spec, catalogue, kinds)
        for name, spec in (document.get("evaluators") or {}).items()
    }
    positions = document.get("positions") or {}
    _only(positions, frozenset(ON_FAIL), "positions")
    return Plan(
        evaluators=built,
        reply=_placed("reply", positions.get("reply") or [], 1.0, built),
        online=_placed("online", *_run(positions, "online"), built),
        release=_placed("release", *_run(positions, "release"), built),
    )


def _only(found: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if not isinstance(found, Mapping):
        raise PlanRefused(f"{where}: expected a mapping, got {type(found).__name__}")
    unknown = sorted(set(found) - allowed)
    if unknown:
        raise PlanRefused(
            f"{where}: unknown field {unknown[0]!r}; allowed: {', '.join(sorted(allowed))}"
        )


def _evaluator(
    name: str, spec: Any, catalogue: Mapping[str, RuleSpec], kinds: Registry
) -> Evaluator:
    if not isinstance(spec, Mapping) or "kind" not in spec:
        raise PlanRefused(f"evaluator {name!r}: needs a `kind`")
    try:
        kind = kinds.load("kind", str(spec["kind"]))
    except UnknownName as exc:
        raise PlanRefused(f"evaluator {name!r}: {exc}") from None
    _only(spec, kind.fields | {"kind"}, f"evaluator {name!r} (kind {spec['kind']})")
    try:
        built: Evaluator = kind.build(name, spec, catalogue)
    except NotBuilt:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise PlanRefused(str(exc).strip("'\"")) from None
    return built


def _run(positions: Mapping[str, Any], position: str) -> tuple[list[Any], float]:
    held = positions.get(position) or {}
    _only(held, frozenset({"run", "sample"} if position == "online" else {"run"}), position)
    return list(held.get("run") or []), float(held.get("sample", 1.0))


def _placed(
    position: str, entries: list[Any], sample: float, built: Mapping[str, Evaluator]
) -> tuple[Placed, ...]:
    placed = []
    for entry in entries:
        where = f"positions.{position}"
        _only(entry, ENTRY_FIELDS[position], where)
        name = entry.get("use")
        if name not in built:
            raise PlanRefused(f"{where}: {name!r} is not declared under `evaluators`")
        evaluator = built[name]
        placed.append(_checked(position, entry, evaluator, sample))
    return tuple(placed)


def _checked(
    position: str, entry: Mapping[str, Any], evaluator: Evaluator, sample: float
) -> Placed:
    where, name = f"positions.{position}", evaluator.name
    on_fail = str(entry.get("on_fail", ON_FAIL[position][0]))
    if on_fail not in ON_FAIL[position]:
        raise PlanRefused(
            f"{where}: {name} on_fail {on_fail!r}; allowed: {', '.join(ON_FAIL[position])}"
        )
    missing = evaluator.needs - PROVIDES[position]
    if missing:
        raise PlanRefused(
            f"{where}: {name} needs {', '.join(sorted(missing))}, which {position} cannot provide"
        )
    max_ms = entry.get("max_ms", REPLY_MAX_MS if position == "reply" else None)
    if position == "reply" and (
        evaluator.speed != "inline" or evaluator.expected_ms > float(max_ms)
    ):
        raise PlanRefused(
            f"{where}: {name} is {evaluator.speed}, ~{evaluator.expected_ms:g} ms; "
            f"reply allows inline evaluators within {float(max_ms):g} ms"
        )
    minimum = entry.get("min", 1.0)
    if not isinstance(minimum, (int, float)) or not 0.0 <= float(minimum) <= 1.0:
        raise PlanRefused(f"{where}: {name} min {minimum!r}: a pass rate between 0 and 1")
    return Placed(
        evaluator,
        on_fail,
        sample=float(entry.get("sample", sample)),
        max_ms=None if max_ms is None else float(max_ms),
        min=float(minimum),
    )


__all__ = [
    "EVERY_ROUTE",
    "KINDS",
    "PROVIDES",
    "REPLY_MAX_MS",
    "EvaluatorFailed",
    "Gate",
    "Placed",
    "Plan",
    "PlanRefused",
    "load",
    "parse",
]
