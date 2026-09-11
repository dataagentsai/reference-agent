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

import ast
import re
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


REALISATION = re.compile(
    r"(InMemory|Postgres|File)\w+|GroqClient|ScriptedClient|MCPToolClient|MCPTransport"
    r"|Recorder|Player"
)


def test_only_the_composition_root_constructs_a_realisation() -> None:
    """A store, client or transport is constructed only where it is defined —
    the package receives its collaborators, it never fetches them. Tests and
    scripts are composition roots and may build what they like; `src` may not.
    """
    defined: dict[str, Path] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef) and REALISATION.fullmatch(node.name):
                defined[node.name] = path

    offences = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in defined and defined[name] != path:
                offences.append(f"{path.relative_to(ROOT)}:{node.lineno} constructs {name}")
    assert defined, "the realisation pattern matched nothing — the check has gone quiet"
    assert offences == [], "\n".join(offences)
