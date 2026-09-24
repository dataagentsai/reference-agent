"""The binding, checked against the catalog it claims to bind.

`harness-profile.yaml` is this agent's binding spec — the ABS the family's
charter describes, which turned out not to need inventing because AHC already
publishes the format. A profile nobody validates is a hopeful document, so:

The check that earns its place is the last one. **Every capability this shape
owes is accounted for** — exercised by a test, declared as an accepted gap, or
listed as believed-met-but-untested. "The agent implements all of AHC" stops
being a claim and becomes arithmetic, and a capability added to the catalog
tomorrow fails this until somebody decides which of the three it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from evals import profile as prof
from evals import statements

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "harness-profile.yaml"
CATALOG = ROOT.parent / "ai-harness-catalog"
MAP = ROOT / "evals" / "assurance-map.json"

needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")


@pytest.fixture(scope="module")
def profile() -> dict:
    """The profile as it actually stands — resolved against the stack it extends.

    T-033. Reading the file raw would report every port the stack binds as
    unbound, because the file names none of them any more: the whole value of
    `extends` is that a product appears once, in the stack, and an agent's
    profile says only what is its own.
    """
    return prof.resolve(PROFILE)


@pytest.fixture(scope="module")
def declared() -> dict:
    """The file itself, unresolved. Only the tests that are *about* inheritance
    should use this one."""
    return yaml.safe_load(PROFILE.read_text())


@needs_catalog
@pytest.mark.discharges("B7")
def test_the_profile_validates_against_the_published_schema(profile: dict) -> None:
    import jsonschema

    schema = json.loads((CATALOG / "schema" / "profile.schema.json").read_text())
    jsonschema.validate(profile, schema)


@needs_catalog
@pytest.mark.tooling
def test_every_bound_port_is_a_port_the_catalog_declares(profile: dict) -> None:
    declared = {p.stem for p in (CATALOG / "ports").glob("*.yaml")}
    bound = set(profile["bindings"])
    assert bound <= declared, f"bound to something that is not a port: {sorted(bound - declared)}"


@needs_catalog
@pytest.mark.tooling
def test_every_capability_named_in_the_profile_exists(profile: dict) -> None:
    known = {p.stem for p in (CATALOG / "capabilities").glob("AHC-*.yaml")}
    named = {gap["capability"] for gap in profile["accepted_gaps"]} | set(profile["x_untested"])
    named |= {key.split("/", 1)[0] for key in profile["decisions"]}
    assert named <= known, f"names capabilities that do not exist: {sorted(named - known)}"


@needs_catalog
@pytest.mark.discharges("B7")
def test_every_capability_this_shape_owes_is_accounted_for(profile: dict) -> None:
    """Exercised, accepted as a gap, or believed met and untested — and nothing
    in two of those at once, because a capability cannot be both owed-and-absent
    and quietly present."""
    owed = {s.id for s in statements.load().owed("AHC")}
    exercised = set(json.loads(MAP.read_text())["by_statement"]) & owed
    gaps = {gap["capability"] for gap in profile["accepted_gaps"]}
    untested = set(profile["x_untested"])

    overlap = (gaps & exercised) | (untested & exercised) | (gaps & untested)
    assert overlap == set(), f"accounted for twice, and inconsistently: {sorted(overlap)}"

    unaccounted = owed - exercised - gaps - untested
    assert unaccounted == set(), (
        "owed by this shape and mentioned nowhere — each is exercised, an accepted "
        f"gap, or untested, and somebody has to say which: {sorted(unaccounted)}"
    )


@pytest.mark.discharges("AHC-0040")
def test_the_scopes_the_binding_declares_are_the_scopes_the_code_uses(profile: dict) -> None:
    """One rule, two places it has to be written — the profile a reviewer reads
    and the map the projection is handed. A check is what keeps that honest."""
    from support_agent.binding import SCOPES

    assert profile["bindings"]["tool_runtime"]["x_scopes"] == SCOPES


@needs_catalog
@pytest.mark.tooling
def test_the_catalog_versions_are_the_ones_on_disk(profile: dict) -> None:
    """A profile pinned to a version nobody is running is a profile that agrees
    with a catalog it has not read."""
    ahc = json.loads((CATALOG / "package.json").read_text())["version"]
    assert profile["catalog"]["ahc"] == ahc, "the profile pins an AHC version that is not here"


# --------------------------------------------------------------------------- #
# Inheritance — T-033.
#
# `extends` was in the published schema from the first version and nothing
# implemented it, so this profile restated the whole stack. Restated values
# drift, and these had: seven bindings disagreed with the stack file they were
# copied from, and `workflow` was missing here entirely. Nothing could notice,
# because nothing had ever compared the two.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("B7")
def test_the_profile_names_the_stack_it_runs_on(declared: dict) -> None:
    """Without this line the file is a copy, and a copy is what drifted."""
    assert declared.get("extends"), "the profile must extend the stack it runs on"
    assert (PROFILE.parent / declared["extends"]).resolve().is_file()


@pytest.mark.discharges("B7")
def test_no_product_is_named_twice(declared: dict) -> None:
    """A product appears in the stack file or in an override that says why, and
    nowhere else.

    An `adapter` restated here is a line that goes stale in silence: the stack
    moves, this does not follow, and the profile keeps claiming the old one. An
    `adapter` that *differs* is a system that left its baseline, which is
    allowed and must carry `x_why`. Either way, a bare adapter here is wrong.
    """
    offences = [
        f"{port}: {field}"
        for port, spec in declared["bindings"].items()
        if isinstance(spec, dict)
        for field in ("approach", "adapter")
        if field in spec and "x_why" not in spec
    ]
    assert offences == [], (
        "named in the agent's profile without a reason to leave the stack: "
        f"{offences} — inherit it, or say why not"
    )


@pytest.mark.discharges("B7")
def test_resolving_actually_supplies_the_stack(profile: dict, declared: dict) -> None:
    """The mechanism works, stated as the thing a reader would doubt.

    `workflow` is the case that made it worth writing: it was absent here
    altogether, so the agent's own conformance report could not see a port every
    approval it raises depends on. It appears below now — but only as the fact
    that other approaches could fill it. The product is still the stack's.
    """
    assert declared["bindings"]["workflow"] == {"x_could_be": ["platform"]}
    assert profile["bindings"]["workflow"]["adapter"] == "temporal"
    assert profile["harness"]["loop"]["owner"] == "in-house"

    # And what the agent adds survives the merge, beside what it inherited.
    tools = profile["bindings"]["tool_runtime"]
    assert tools["adapter"] == "mcp-client", "inherited from the stack"
    assert tools["x_scopes"]["issue_refund"] == "refunds:write", "this agent's own"


@pytest.mark.tooling
@needs_catalog
def test_this_resolver_agrees_with_the_catalogs_own(profile: dict) -> None:
    """Two implementations of one rule, pinned against each other.

    The normative resolver is the catalog's, because `extends` belongs to the
    published format. This repository implements it again rather than making a
    Node tool a runtime dependency of every test run — and two implementations
    that agree because nobody looked are not the same as two that agree.

    Skipped rather than failed where Node is absent: the pin is worth having and
    is not worth making the suite unrunnable for.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    done = subprocess.run(
        [node, str(CATALOG / "tools" / "resolve.js"), str(PROFILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert yaml.safe_load(done.stdout) == profile, "the two resolvers disagree"
