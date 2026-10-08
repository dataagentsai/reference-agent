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
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tooling

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent
SRC = ROOT / "src" / "support_agent"
LIB = ROOT / "packages" / "agent-harness" / "src" / "agent_harness"
"""The harness, extracted (T-019). Every check below that reads the package
reads both trees: a ceiling the library escaped by moving would be a ceiling
that stopped holding the code it was written for."""


def sources() -> list[Path]:
    return [*SRC.rglob("*.py"), *LIB.rglob("*.py")]


CHECKS = [
    ("strict types", [str(BIN / "mypy")]),
    ("import contract", [str(BIN / "lint-imports")]),
    (
        "lint, complexity and size ceilings",
        [str(BIN / "ruff"), "check", "src", "tests", "evals", "packages"],
    ),
    ("format", [str(BIN / "ruff"), "format", "--check", "src", "tests", "evals", "packages"]),
]


@pytest.mark.parametrize(("name", "command"), CHECKS, ids=[c[0] for c in CHECKS])
def test_the_build_check_passes(name: str, command: list[str]) -> None:
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"{name} failed:\n{done.stdout[-3000:]}{done.stderr[-1000:]}"


RATCHETS = [
    # (what, measured, ceiling) — lower the ceiling when the value falls; never raise it.
    (
        "type: ignore comments in the package",
        lambda: sum(p.read_text().count("type: ignore") for p in sources()),
        2,
    ),
    (
        "lines in the longest module",
        lambda: max(len(p.read_text().splitlines()) for p in sources()),
        416,  # loop — was 744 (the entrypoint) before G0.4
    ),
]


@pytest.mark.parametrize(("what", "measure", "ceiling"), RATCHETS, ids=[r[0] for r in RATCHETS])
def test_the_ratchet_holds(what: str, measure, ceiling: int) -> None:
    value = measure()
    assert value <= ceiling, f"{what}: {value}, ceiling {ceiling} — the ratchet only turns one way"


REALISATION = re.compile(
    r"(InMemory|Postgres|File)\w+|GroqClient|PydanticAIClient|ScriptedClient|MCPToolClient"
    r"|MCPTransport|EntraOnBehalfOf|DBOS(Approvals|ApprovalDesk|Escalations|EscalationDesk)"
    r"|Recorder|Player"
)


@pytest.mark.discharges("B13", "AHC-0004")
def test_only_the_composition_root_constructs_a_realisation() -> None:
    """A store, client or transport is constructed only where it is defined —
    the package receives its collaborators, it never fetches them. Tests and
    scripts are composition roots and may build what they like; `src` may not.

    AHC-0004's second sentence, checked rather than asserted: provider SDKs
    are constructed in one place and nowhere else. `GroqClient` is in the
    realisation set above, so a second construction site fails this.
    """
    defined: dict[str, Path] = {}
    for path in sources():
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef) and REALISATION.fullmatch(node.name):
                defined[node.name] = path

    offences = []
    for path in sources():
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
        "synthetic_customers",
        "config",
        "system_prompt",
        "history_chars",
        "rules",
        "policy_rules",
        "tier_2",
        "fresh_for_s",  # AHC-0107's window: the binding's number, not machinery
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


@pytest.mark.discharges("AHC-0110")
def test_every_failure_this_package_declares_says_what_kind_it_is() -> None:
    """AHC-0110's third decision, made true rather than intended.

    Fourteen exception types across ten modules, each named well for where it
    lives and no two named the same way. The set was unusable from above: a
    caller's handler is a chain of `isinstance` checks against a list somebody
    assembled by reading the code, wrong the moment a module adds a fifteenth —
    and silently, because the new one falls through to whatever the last branch
    does.

    So the fifteenth cannot be added without a kind. Convention plus review
    would be free and would decay: the person adding it has not read the first
    fourteen, which is the whole reason they are inconsistent today.

    `ApprovalRequested` is exempt and says so in its own source: it is control
    flow rather than a failure — the loop catches it and returns a typed
    `NeedsApproval` — and giving it a failure kind would file a human being
    asked to decide something under the same heading as a provider timing out.
    """
    import inspect
    import pkgutil

    import agent_harness
    import support_agent
    from support_agent.contracts.failures import AgentFailure

    # Control flow that happens to be spelled as an exception. Each is caught by
    # the layer directly above the one that raises it and turned into a typed
    # result; none ever reaches a caller as a failure, and giving them a failure
    # kind would file "a person is being asked to decide" under the same heading
    # as a provider timing out.
    control_flow = {
        "ApprovalRequested",  # the loop catches it and returns NeedsApproval
        "RefundRequested",  # the same signal, carrying this shop's wording
        "ActionDeclined",  # the loop catches it and returns Failed, typed `declined` (T-095)
        "_Retry",  # inside the retry wrapper, never leaves it
        "_NotYours",  # the HTTP edge turns it into a 404, never a 5xx
    }
    undeclared: list[str] = []
    walked = [
        *pkgutil.walk_packages(support_agent.__path__, "support_agent."),
        *pkgutil.walk_packages(agent_harness.__path__, "agent_harness."),
    ]
    for module in walked:
        imported = importlib.import_module(module.name)
        for name, obj in vars(imported).items():
            if not inspect.isclass(obj) or not issubclass(obj, BaseException):
                continue
            if obj.__module__ != module.name or name in control_flow:
                continue
            if not issubclass(obj, AgentFailure):
                undeclared.append(f"{module.name}.{name}")

    assert undeclared == [], (
        "these failures carry no kind from the shared vocabulary — a caller "
        f"cannot decide whether trying again is sensible: {sorted(undeclared)}"
    )


@pytest.mark.discharges("AHC-0018", "AAC-0011")
def test_every_counter_declared_is_incremented_somewhere() -> None:
    """A metric nobody writes to is a dashboard panel that reads zero forever.

    Worse than a missing panel, because a flat line is read as *this never
    happens* rather than as *nobody is counting*. The check is the same shape as
    the span contract's: enumerate what the module declares, then find where the
    package writes to it.

    It is deliberately a search over source rather than a run, because the
    interesting case is the counter that exists and is reached only on a path no
    test drives — which a run would report as passing.
    """
    from support_agent.telemetry import counters as declared

    source = "\n".join(p.read_text() for p in sources() if p.name != "counters.py")
    # Functions — `record_turn`, `bind`, the configuration label's two — are
    # not instruments.
    names = [n for n in declared.__all__ if n.islower() and not callable(getattr(declared, n))]

    # `record_turn` writes to three of them from inside the module itself, which
    # the source search above cannot see because it excludes that file.
    written_inside = {"turns", "turn_duration", "refusals", "escalations"}
    unwritten = [
        name for name in names if name not in written_inside and f"counters.{name}." not in source
    ]
    assert unwritten == [], (
        f"declared and never incremented — a panel that reads zero forever: {unwritten}"
    )
