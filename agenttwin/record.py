"""The run record — what a verdict needs to be interpretable.

`(world₀, scenario, agent) → (world₁, trace, verdict)`. This is the right-hand
side, and the part that makes it worth anything is the **diff**.

A world-state diff is the strongest oracle available, and one no transcript
grading can produce. "Exactly one refund row exists" is a statement about world₁.
"The agent said it refunded once" is a statement about prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agenttwin.world import Resolution


@dataclass(frozen=True)
class Change:
    entity: str
    key: str
    field: str
    before: Any
    after: Any

    def __str__(self) -> str:
        return f"{self.entity}/{self.key}.{self.field}: {self.before!r} -> {self.after!r}"


def diff(before: dict, after: dict) -> tuple[Change, ...]:
    """Every field that moved. Additions and removals count as changes too —
    a row that appeared is the most important kind of change there is."""
    changes: list[Change] = []
    for entity in sorted(set(before) | set(after)):
        rows_before = before.get(entity, {})
        rows_after = after.get(entity, {})
        for key in sorted(set(rows_before) | set(rows_after)):
            row_before = rows_before.get(key, {})
            row_after = rows_after.get(key, {})
            for name in sorted(set(row_before) | set(row_after)):
                old, new = row_before.get(name), row_after.get(name)
                if old != new:
                    changes.append(Change(entity, key, name, old, new))
    return tuple(changes)


@dataclass
class RunRecord:
    """Everything needed to interpret, reproduce and re-run one scenario."""

    scenario: str
    world: str
    seed: int
    resolution: Resolution
    config_fingerprint: str = ""
    determinism_class: str = "scripted"
    """The weakest class of any actor in the run. A world containing a
    model-driven actor is not reproducible, and the record says so rather than
    letting a reader assume it is."""

    changes: tuple[Change, ...] = ()
    effects: tuple[tuple[str, str], ...] = ()
    verdicts: dict[str, bool] = field(default_factory=dict)
    discharges: tuple[str, ...] = ()
    reply: str = ""
    termination: str = ""

    @property
    def passed(self) -> bool:
        return all(self.verdicts.values())

    def render(self) -> str:
        lines = [
            f"scenario     {self.scenario}",
            f"world        {self.world} (seed {self.seed}, {self.resolution})",
            f"determinism  {self.determinism_class}",
            f"config       {self.config_fingerprint or '-'}",
            f"termination  {self.termination or '-'}",
            f"discharges   {', '.join(self.discharges) or '-'}",
            "world diff:",
        ]
        lines += [f"  {c}" for c in self.changes] or ["  (nothing changed)"]
        lines += ["verdicts:"]
        lines += [f"  {'PASS' if ok else 'FAIL'}  {name}" for name, ok in self.verdicts.items()]
        return "\n".join(lines)


__all__ = ["Change", "RunRecord", "diff"]
