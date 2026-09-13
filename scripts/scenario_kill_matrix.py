"""Does each scenario test what its name claims?

A suite-wide mutation run answers a different question — *does anything catch
this change* — and for a well-covered module the answer is yes while an
individual scenario sitting inside that suite tests nothing at all. That is not
hypothetical. `nobody-picks-up-the-escalation` is named "and the customer is
told", discharges `P-ESC-TOLD` and `P-ESC-LAPSE`, and asserted only that two
things did *not* happen. Disabling the lapse entirely left it green; three unit
tests and one other scenario went red. It had been lying in its own title for
weeks, and nothing could have noticed, because every tool that looks at coverage
looks at the suite.

So this runs each scenario **alone**.

    for each branch in the code
        invert it
        run all 30 scenarios, one at a time
        record which of them fail

Two questions fall out of the matrix, and they are different questions:

**Which scenarios kill nothing?** Those are the ones to read again. A scenario
that survives every inversion of every branch is asserting something no change
to the code can disturb — usually because every check it makes is negative, and
a negative check passes hardest when the code does nothing at all.

**Which inversions does nobody kill?** Branches the whole scenario suite steps
over. Some are honest — unreachable defensive guards, telemetry — and some are
a capability everyone believes is exercised.

Branch inversion is the only operator here, on the evidence of a full Cosmic Ray
run over `entrypoint/handoff.py`: of 69 surviving mutants, 57 were `|` inside
type annotations that `from __future__ import annotations` never evaluates, 7
were arithmetic on a telemetry value, and the rest were equivalent. Every
inversion was killed. The arithmetic operators produce noise in this codebase at
about eight to one; inversions produce signal.

    uv run python scripts/scenario_kill_matrix.py
    uv run python scripts/scenario_kill_matrix.py --modules escalation entrypoint
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "src"
PKG = SRC / "support_agent"
SCENARIOS = ROOT / "scenarios"

CHILD = "AGENT_KILL_MATRIX_CHILD"

# `if x:` and `elif x:`, on one line, with or without a trailing comment. A
# condition spread over several lines is skipped rather than guessed at: a
# mutation that does not parse is a mutation every scenario "kills", which reads
# as coverage nobody has.
BRANCH = re.compile(r"^(?P<indent>\s*)(?P<kw>if|elif) (?P<cond>.+?):(?P<tail>\s*(#.*)?)$")

# Modules whose branches a scenario could plausibly reach. The tool boundary and
# the provider adapter are driven by the world and the scripted model, not by
# the agent's own decisions, so inverting their branches tests the harness.
DEFAULT_MODULES = ("entrypoint", "escalation", "approvals", "router", "policy", "loop")


class Mutant:
    """One inverted branch, and where it lives."""

    def __init__(self, path: pathlib.Path, lineno: int, before: str, after: str):
        self.path, self.lineno, self.before, self.after = path, lineno, before, after

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(SRC)}:{self.lineno}"

    def __str__(self) -> str:
        return f"{self.where}  {self.before.strip()}"


def mutants_in(path: pathlib.Path) -> list[Mutant]:
    found = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        m = BRANCH.match(line)
        if not m:
            continue
        cond = m["cond"]
        if cond.strip() in ("True", "False", "TYPE_CHECKING"):
            # A constant is a duller test, and `TYPE_CHECKING` is false at
            # runtime by definition — no scenario can ever kill it, so listing
            # it as uncovered is noise in the one report that must not have any.
            continue
        flipped = f"{m['indent']}{m['kw']} not ({cond}):{m['tail']}"
        found.append(Mutant(path, i, line, flipped))
    return found


def collect(modules: tuple[str, ...]) -> list[Mutant]:
    out: list[Mutant] = []
    for name in modules:
        target = PKG / name
        files = sorted(target.rglob("*.py")) if target.is_dir() else [PKG / f"{name}.py"]
        for path in files:
            if "__pycache__" not in str(path):
                out.extend(mutants_in(path))
    return out


# ------------------------------------------------------------------ the child


async def _run_one(path: pathlib.Path) -> bool:
    """One scenario against whatever `support_agent` is importable. True = it failed."""
    from agenttwin import Clock, Live, load, perturbed
    from agenttwin.scenario_file import load_scenario
    from agenttwin.suite import provider_faults, run_file, timeline_for
    from evals.simulation import subject_for
    from tests.test_scenario_files import model_for

    scenario = load_scenario(path)
    live = Live.start(load(path.parent / scenario.world))
    timeline = timeline_for(scenario)
    wrap = perturbed(live, timeline)
    clock = Clock(step_s=scenario.step_seconds)
    async with subject_for(
        live,
        llm=model_for(path.stem),
        wrap=wrap,
        provider_faults=provider_faults(scenario),
        clock=clock,
    ) as subject:
        _, outcomes = await run_file(
            path, subject=subject, live=live, timeline=timeline, clock=clock
        )
    return any(not o.passed for o in outcomes)


def child(paths: list[pathlib.Path]) -> int:
    """Run every scenario in one interpreter and say which failed.

    One process for all of them, because the mutation is baked into the source
    this interpreter imported and re-importing it per scenario would cost more
    than the scenarios do.
    """
    for path in paths:
        try:
            failed = asyncio.run(_run_one(path))
        except Exception:  # noqa: BLE001 — a mutant that crashes a scenario killed it
            failed = True
        print(f"{path.stem}\t{'KILL' if failed else 'live'}", flush=True)
    return 0


# ----------------------------------------------------------------- the parent


def run_against(mutant: Mutant | None, paths: list[pathlib.Path]) -> dict[str, bool]:
    """Apply one mutation to a throwaway copy of the tree and run every scenario."""
    with tempfile.TemporaryDirectory() as tmp:
        staged = pathlib.Path(tmp) / "src"
        shutil.copytree(SRC, staged, ignore=shutil.ignore_patterns("__pycache__"))
        if mutant is not None:
            target = staged / mutant.path.relative_to(SRC)
            lines = target.read_text(encoding="utf-8").splitlines()
            lines[mutant.lineno - 1] = mutant.after
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")

        env = {
            **os.environ,
            CHILD: "1",
            # The staged copy shadows the editable install, so the child imports
            # the mutation and everything else unchanged.
            "PYTHONPATH": f"{staged}{os.pathsep}{ROOT}",
        }
        done = subprocess.run(
            [sys.executable, __file__, "--child", *[str(p) for p in paths]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=900,
        )
    verdicts = {}
    for line in done.stdout.splitlines():
        if "\t" in line:
            stem, verdict = line.rsplit("\t", 1)
            verdicts[stem] = verdict == "KILL"
    return verdicts


def passing(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """The scenarios green against unmutated code.

    A scenario already failing kills every mutant trivially, which would read as
    the most rigorous file in the suite.
    """
    baseline = run_against(None, paths)
    already = sorted(s for s, failing in baseline.items() if failing)
    if already:
        print(f"  {len(already)} already failing — excluded: {', '.join(already)}\n")
    return [p for p in paths if not baseline.get(p.stem, True)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Which scenarios test what they claim?")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--modules", nargs="*", default=list(DEFAULT_MODULES))
    ap.add_argument("--limit", type=int, default=0, help="stop after this many mutants")
    args, rest = ap.parse_known_args()

    paths = sorted(SCENARIOS.glob("*.yaml"))
    if args.child:
        return child([pathlib.Path(p) for p in rest])

    mutants = collect(tuple(args.modules))
    if args.limit:
        mutants = mutants[: args.limit]

    print(f"{len(mutants)} branch inversions × {len(paths)} scenarios\n")

    live_paths = passing(paths)

    killed_by: dict[str, set[str]] = {p.stem: set() for p in live_paths}
    unkilled: list[Mutant] = []
    for n, mutant in enumerate(mutants, start=1):
        verdicts = run_against(mutant, live_paths)
        killers = [stem for stem, died in verdicts.items() if died]
        for stem in killers:
            killed_by[stem].add(mutant.where)
        if not killers:
            unkilled.append(mutant)
        print(f"  [{n:>3}/{len(mutants)}] {mutant.where:<46} killed by {len(killers)}")

    report(killed_by, unkilled, scenarios=len(live_paths), mutants=len(mutants))
    return 0


def report(
    killed_by: dict[str, set[str]], unkilled: list[Mutant], *, scenarios: int, mutants: int
) -> None:
    """The two questions, in the order they are worth reading."""
    print("\n" + "=" * 72)
    idle = sorted(stem for stem, kills in killed_by.items() if not kills)
    print(f"\nSCENARIOS THAT KILLED NOTHING — {len(idle)} of {scenarios}")
    print("Read these again. A scenario no change to the code can disturb is")
    print("asserting something the code does not decide.\n")
    for stem in idle:
        print(f"  {stem}")
    if not idle:
        print("  (none — every scenario noticed at least one inverted branch)")

    print(f"\nBRANCHES NO SCENARIO KILLED — {len(unkilled)} of {mutants}")
    print("Some are honest: defensive guards, telemetry, unreachable states.\n")
    for mutant in unkilled[:40]:
        print(f"  {mutant}")
    if len(unkilled) > 40:
        print(f"  … and {len(unkilled) - 40} more")

    print("\nWEAKEST SCENARIOS BY KILL COUNT\n")
    for stem, kills in sorted(killed_by.items(), key=lambda kv: len(kv[1]))[:10]:
        print(f"  {len(kills):>3}  {stem}")


if __name__ == "__main__":
    raise SystemExit(main())
