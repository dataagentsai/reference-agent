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
from evals import statements

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "harness-profile.yaml"
CATALOG = ROOT.parent / "ai-harness-catalog"
MAP = ROOT / "evals" / "assurance-map.json"

needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")


@pytest.fixture(scope="module")
def profile() -> dict:
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


@needs_catalog
@pytest.mark.tooling
def test_the_catalog_versions_are_the_ones_on_disk(profile: dict) -> None:
    """A profile pinned to a version nobody is running is a profile that agrees
    with a catalog it has not read."""
    ahc = json.loads((CATALOG / "package.json").read_text())["version"]
    assert profile["catalog"]["ahc"] == ahc, "the profile pins an AHC version that is not here"
