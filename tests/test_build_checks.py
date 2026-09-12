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
        416,  # loop — was 744 (the entrypoint) before G0.4
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


@pytest.mark.discharges("B13")
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


@pytest.mark.discharges("B13")
def test_every_collaborator_the_root_takes_is_an_interface() -> None:
    """B13's first half, which the check above does not cover: a port typed as a
    concrete class is a deployment nobody can swap for a simulated world, a
    recording or a durable store.

    The exceptions are named rather than tolerated. `Meter` is a concrete class
    behind a factory because a meter is made per unit of work and its
    construction is where an unpriced model fails (AHC-0101); the rest of the
    signature is configuration — versioned data, not somebody else's machinery.
    """
    import inspect
    import typing

    from support_agent import entrypoint as ep
    from support_agent.contracts import protocols

    ports = {
        name
        for name, obj in vars(protocols).items()
        if inspect.isclass(obj)
        and typing.get_type_hints(obj, include_extras=True) is not None
        and getattr(obj, "_is_protocol", False)
    }
    configuration = {  # versioned data this agent owns, not a collaborator
        "capacity",
        "config",
        "system_prompt",
        "history_chars",
        "rules",
        "policy_rules",
        "tier_2",
    }
    factories = {"metering"}  # made per unit of work

    import re

    ports |= {"DeliveryLog"}  # declared beside its module rather than in contracts
    concrete = []
    for name, parameter in inspect.signature(ep.build).parameters.items():
        if name in configuration or name in factories:
            continue
        # Tokens, never substrings: `InMemoryCheckpointStore` contains
        # `CheckpointStore`, so a containment test passes the exact thing this
        # is looking for.
        named = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", str(parameter.annotation)))
        if not named & ports:
            concrete.append(f"{name}: {parameter.annotation}")

    assert concrete == [], "the root takes a concrete collaborator:\n" + "\n".join(concrete)
