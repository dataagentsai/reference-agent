"""G0.3 — the build-time checks, run with the suite so they cannot be skipped.

Catching at build time what a test catches only if it happens to run the line:
a component that does not satisfy its interface, a dependency pointing the wrong
way, a function growing into the next god object. Each check here is a
deterministic tool, run on every `pytest`, because a check that lives only in a
README is a check nobody runs.

The ceilings are a ratchet: today's worst value, lowered as the decomposition
takes the worst offenders apart. Tooling — these test the build, not the agent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tooling

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent
SRC = ROOT / "src" / "support_agent"

CHECKS = [
    ("strict types", [str(BIN / "mypy")]),
    ("import contract", [str(BIN / "lint-imports")]),
    ("lint, complexity and size ceilings", [str(BIN / "ruff"), "check", "src", "tests", "evals"]),
    ("format", [str(BIN / "ruff"), "format", "--check", "src", "tests", "evals"]),
]


@pytest.mark.parametrize(("name", "command"), CHECKS, ids=[c[0] for c in CHECKS])
def test_the_build_check_passes(name: str, command: list[str]) -> None:
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"{name} failed:\n{done.stdout[-3000:]}{done.stderr[-1000:]}"


RATCHETS = [
    # (what, measured, ceiling) — lower the ceiling when the value falls; never raise it.
    (
        "type: ignore comments in the package",
        lambda: sum(p.read_text().count("type: ignore") for p in SRC.rglob("*.py")),
        2,
    ),
    (
        "lines in the longest module",
        lambda: max(len(p.read_text().splitlines()) for p in SRC.rglob("*.py")),
        449,  # telemetry — was 744 (the entrypoint) before G0.4
    ),
]


@pytest.mark.parametrize(("what", "measure", "ceiling"), RATCHETS, ids=[r[0] for r in RATCHETS])
def test_the_ratchet_holds(what: str, measure, ceiling: int) -> None:
    value = measure()
    assert value <= ceiling, f"{what}: {value}, ceiling {ceiling} — the ratchet only turns one way"
