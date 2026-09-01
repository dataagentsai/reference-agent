"""Which obligations this suite actually exercises.

The eval harness's reporting half. A test declares what it discharges:

    @discharges("AAC-0055", "AAC-0054")
    async def test_the_step_budget_terminates_and_says_why(): ...

and the run produces a report naming what passed, what failed, and — the part
that matters — **what was never exercised at all.**

An unexercised obligation is the honest output of this system. A report that
lists only passes and failures implies the remainder is fine; a report that names
the gap turns an undiscovered hole into an identified, owned one, which is the
difference between a finding and a conformant gap under a management-system
audit.

Nothing here restates the catalog. `evals/a6_obligations.json` carries
identifiers and metadata; the normative statement of each obligation stays where
it was written, and this module only ever names ids.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[2] / "evals" / "a6_obligations.json"


class Verdict(StrEnum):
    """Deliberately four values, not two.

    `SCORED` exists because a fix that moves a model-driven case from 60% to 80%
    is real progress and invisible to a single pass/fail. Mixing binary and
    scored verdicts in one column is how a team ends up chasing flakes instead of
    bugs, so they are never summed together.

    `NOT_EXERCISED` is the one that makes the report worth reading.
    """

    PASSED = "passed"
    FAILED = "failed"
    SCORED = "scored"
    NOT_EXERCISED = "not_exercised"


@dataclass(frozen=True)
class Obligation:
    id: str
    title: str
    dimension: str
    level: str
    gate: bool
    stages: tuple[str, ...]
    mechanisms: tuple[str, ...]


@dataclass
class Coverage:
    obligation: Obligation
    verdict: Verdict = Verdict.NOT_EXERCISED
    tests: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def load(path: Path | str = MANIFEST) -> tuple[Obligation, ...]:
    raw = json.loads(Path(path).read_text())
    return tuple(
        Obligation(
            id=o["id"],
            title=o["title"],
            dimension=o["dimension"],
            level=o["level"],
            gate=o["gate"],
            stages=tuple(o["stages"]),
            mechanisms=tuple(o["mechanisms"]),
        )
        for o in raw["obligations"]
    )


def load_elsewhere(path: Path | str = MANIFEST) -> dict[str, dict]:
    """Obligations that exist in the catalog but are not tagged A6."""
    return json.loads(Path(path).read_text()).get("elsewhere_in_catalog", {})


class Report:
    """Accumulates outcomes, then answers what was covered."""

    def __init__(self, obligations: Iterable[Obligation] | None = None) -> None:
        known = tuple(obligations) if obligations is not None else load()
        self.coverage: dict[str, Coverage] = {o.id: Coverage(o) for o in known}
        self.elsewhere = load_elsewhere()
        self.tagging_gaps: dict[str, list[str]] = {}
        self.unknown_ids: set[str] = set()

    def record(self, obligation_id: str, test: str, *, passed: bool, scored: bool = False) -> None:
        entry = self.coverage.get(obligation_id)
        if entry is None:
            if obligation_id in self.elsewhere:
                # The obligation exists — it is simply not tagged for this
                # archetype. A test discharging it is *evidence* that the tag is
                # missing, not a mistake in the test. This is how the harness
                # produces the case for a catalog change rather than someone
                # arguing for one.
                self.tagging_gaps.setdefault(obligation_id, []).append(test)
                return
            # An id that exists nowhere is a defect in the test, surfaced
            # rather than silently dropped.
            self.unknown_ids.add(obligation_id)
            return
        entry.tests.append(test)
        if not passed:
            entry.verdict = Verdict.FAILED
            entry.failures.append(test)
        elif entry.verdict is not Verdict.FAILED:
            entry.verdict = Verdict.SCORED if scored else Verdict.PASSED

    def by_verdict(self, verdict: Verdict) -> tuple[Coverage, ...]:
        return tuple(c for c in self.coverage.values() if c.verdict is verdict)

    @property
    def exercised(self) -> int:
        return sum(1 for c in self.coverage.values() if c.verdict is not Verdict.NOT_EXERCISED)

    @property
    def gates_uncovered(self) -> tuple[Coverage, ...]:
        """Release-gating obligations with nothing behind them.

        Worth separating: an uncovered `gate: true` obligation is a claim the
        release process makes and the test suite cannot support.
        """
        return tuple(
            c
            for c in self.coverage.values()
            if c.obligation.gate and c.verdict is Verdict.NOT_EXERCISED
        )

    def render(self) -> str:
        total = len(self.coverage)
        lines = [
            "",
            "AAC conformance — archetype A6 (tool-using agent)",
            "=" * 64,
            f"  exercised     {self.exercised}/{total}",
            f"  passed        {len(self.by_verdict(Verdict.PASSED))}",
            f"  failed        {len(self.by_verdict(Verdict.FAILED))}",
            f"  scored        {len(self.by_verdict(Verdict.SCORED))}",
            f"  NOT exercised {len(self.by_verdict(Verdict.NOT_EXERCISED))}",
        ]

        failed = self.by_verdict(Verdict.FAILED)
        if failed:
            lines += ["", "  FAILED"]
            lines += [f"    {c.obligation.id}  {c.obligation.title[:58]}" for c in failed]

        gaps = self.gates_uncovered
        if gaps:
            lines += ["", f"  RELEASE GATES WITH NO TEST BEHIND THEM ({len(gaps)})"]
            lines += [f"    {c.obligation.id}  {c.obligation.title[:58]}" for c in gaps]

        rest = [c for c in self.by_verdict(Verdict.NOT_EXERCISED) if not c.obligation.gate]
        if rest:
            lines += ["", f"  not exercised, not gating ({len(rest)})"]
            lines += [
                f"    {c.obligation.id}  {c.obligation.dimension:13} {c.obligation.title[:44]}"
                for c in rest
            ]

        if self.tagging_gaps:
            lines += ["", f"  EXERCISED BUT NOT TAGGED A6 ({len(self.tagging_gaps)})"]
            lines += [
                "    evidence that these apply to a tool-using agent and are"
                " tagged for other shapes only:"
            ]
            for oid in sorted(self.tagging_gaps):
                meta = self.elsewhere[oid]
                arch = ",".join(meta["archetypes"])
                lines.append(f"    {oid}  [{arch:8}] {meta['title'][:48]}")

        if self.unknown_ids:
            lines += ["", "  CLAIMED BUT NOT IN THE CATALOG (fix the test)"]
            lines += [f"    {i}" for i in sorted(self.unknown_ids)]

        lines += ["", "  Identifiers only. Read the obligations in the catalog.", ""]
        return "\n".join(lines)


__all__ = [
    "MANIFEST",
    "Coverage",
    "Obligation",
    "Report",
    "Verdict",
    "load",
    "load_elsewhere",
]
