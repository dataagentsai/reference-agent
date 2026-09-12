"""Which patterns this agent's own specification forces, and which it chose.

The claim worth testing: **most of an agent's patterns are not decisions.** A
deterministic route exists because the specification says which requests are
answerable without the model; an approval gate exists because an operation's
authority says a person decides. Neither was anybody's preference, and writing
them down as choices would be a second statement of a rule that already exists.

So this derives them. Each rule below reads the AOAS and, where the condition
holds, names the pattern it entails **and the statement that entailed it**. What
is left over is the interesting part: a pattern the reference implements that no
statement forces is a genuine decision, and belongs in the profile with its
source rather than in a view like this one.

    uv run python scripts/pattern_view.py            # writes docs/PATTERN-VIEW.md
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from agenttwin.loader import resolve_spec
from agenttwin.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT / "worlds" / "clothing.yaml"


@dataclass(frozen=True)
class Entailment:
    pattern: str
    because: str
    """The statement that forces it, quoted closely enough to find."""


Rule = Callable[[dict], Iterator[Entailment]]


def _deterministic_route(doc: dict) -> Iterator[Entailment]:
    for pid, policy in doc.get("policies", {}).items():
        if pid == "P-DIRECT":
            yield Entailment("PAT-router-ahead-of-loop", f"{pid}: {policy['rule']}")
            yield Entailment(
                "PAT-registry-of-declared-handlers", f"{pid} names the answerable intents"
            )


def _human_authority(doc: dict) -> Iterator[Entailment]:
    for name, op in doc.get("operations", {}).items():
        authority = op.get("authority")
        if isinstance(authority, dict) and authority.get("otherwise") == "human_approval":
            yield Entailment(
                "PAT-approval-that-returns",
                f"{name}.authority: a person decides above the declared conditions",
            )


def _irreversible(doc: dict) -> Iterator[Entailment]:
    for name, op in doc.get("operations", {}).items():
        if op.get("side_effect") == "irreversible":
            yield Entailment(
                "PAT-compensation-for-the-irreversible",
                f"{name} is irreversible, and the step after it can still fail",
            )
        if op.get("identity"):
            yield Entailment(
                "PAT-delivery-id-at-the-edge",
                f"{name}.identity declares what makes a repeat a repeat",
            )


def _escalation(doc: dict) -> Iterator[Entailment]:
    escalation = doc.get("policies", {}).get("escalation", {})
    for statement in escalation.get("statements", []):
        if statement["id"] == "P-ESC-TTL":
            yield Entailment("PAT-escalation-with-lapse", f"P-ESC-TTL: {statement['rule']}")
    if escalation.get("on_condition") or escalation.get("on_request"):
        yield Entailment(
            "PAT-versioned-rule-set", "escalation rules are a list, and first match wins"
        )


def _untrusted(doc: dict) -> Iterator[Entailment]:
    for entity, spec in doc.get("entities", {}).items():
        for field, declared in spec.get("fields", {}).items():
            if declared.get("untrusted"):
                yield Entailment(
                    "PAT-fenced-provenance",
                    f"{entity}.{field} is untrusted — content, never instruction",
                )


def _refusals(doc: dict) -> Iterator[Entailment]:
    if doc.get("purpose", {}).get("refuses"):
        yield Entailment("PAT-versioned-rule-set", "the refusal list is normative and changes")


def _absence(doc: dict) -> Iterator[Entailment]:
    """A capability that can be absent in a deployment needs absence to be a
    realisation rather than a branch — and the spec says which those are by
    giving them an `on_refusal`."""
    for name, op in doc.get("operations", {}).items():
        if op.get("on_refusal"):
            yield Entailment(
                "PAT-port-with-a-null-object",
                f"{name}.on_refusal: what is offered when it cannot be done",
            )


def _bounded_context(doc: dict) -> Iterator[Entailment]:
    for prop in doc.get("required", {}).get("properties", []):
        if prop["id"] == "Q-TOOL-RESULT":
            yield Entailment("PAT-exchange-safe-reduction", f"{prop['id']}: {prop['must']}")


RULES: tuple[Rule, ...] = (
    _deterministic_route,
    _human_authority,
    _irreversible,
    _escalation,
    _untrusted,
    _refusals,
    _absence,
    _bounded_context,
)


def entailed(world: Path = WORLD) -> dict[str, list[str]]:
    """Pattern id → the statements that force it, deduplicated and ordered."""
    doc = load_spec(resolve_spec(world))
    found: dict[str, list[str]] = {}
    for rule in RULES:
        for e in rule(doc):
            reasons = found.setdefault(e.pattern, [])
            if e.because not in reasons:
                reasons.append(e.because)
    return {k: found[k] for k in sorted(found)}


def render(found: dict[str, list[str]]) -> str:
    lines = [
        "# Pattern View",
        "",
        "*Generated by `scripts/pattern_view.py` from this agent's AOAS. Never edit",
        "by hand — regenerate.*",
        "",
        "Which patterns this agent's specification **forces**, and the statement that",
        "forces each. A pattern here was not chosen: it follows from something the",
        "specification already says, and a regeneration that read the same specification",
        "would arrive at it too.",
        "",
        f"**{len(found)} patterns entailed.**",
        "",
    ]
    for pattern, reasons in found.items():
        lines.append(f"### {pattern}")
        lines.append("")
        for reason in reasons:
            lines.append(f"- {reason}")
        lines.append("")
    return "\n".join(lines)


__all__ = ["Entailment", "entailed", "render"]
