"""The agent's AAC coverage report: what the junit carries, and how the report is built.

AAC's junit adapter reads a property named `aac`; this suite's tests name what
they verify in a `discharges` marker that also carries AHC and AOAS ids. The
conftest records the AAC subset under `aac`, and these rows hold what goes in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from evals import statements

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import aac_report  # noqa: E402

pytestmark = pytest.mark.tooling

# (name, the marker's ids, the test's other markers, what `aac` says)
CLAIMS = [
    (
        "AAC ids only, in order",
        ("AAC-0055", "AHC-0041", "Q-STEPS", "AAC-0011"),
        set(),
        "AAC-0055 AAC-0011",
    ),
    ("no AAC id: no property", ("AHC-0096",), set(), ""),
    ("a test documenting a gap is evidence of nothing", ("AAC-0046",), {"documents_gap"}, ""),
    ("an instrument's test is not the agent's", ("AAC-0014",), {"tooling"}, ""),
    ("a component the agent never calls", ("AAC-0047",), {"unwired"}, ""),
    ("a scored test still evidences its ids", ("AAC-0001",), {"scored"}, "AAC-0001"),
]


@pytest.mark.parametrize(("name", "ids", "markers", "aac"), CLAIMS, ids=[c[0] for c in CLAIMS])
def test_the_aac_property_is_the_aac_subset_of_what_a_test_claims(
    name: str, ids: tuple[str, ...], markers: set[str], aac: str
) -> None:
    assert statements.aac_claim(ids, markers) == aac


def test_the_config_handed_to_build_report_is_absolute_and_stamped() -> None:
    import yaml

    config = yaml.safe_load((ROOT / "aac.config.yaml").read_text())
    out = aac_report.resolved(config, "abc1234")
    assert out["subject"]["version"] == "abc1234"
    assert config["subject"]["version"] != "abc1234", "the committed config is not changed"
    for source in out["sources"]:
        assert Path(source["path"]).is_absolute() and str(source["path"]).startswith(str(ROOT))


def test_the_declarations_are_owned_and_dated_like_the_profiles_gaps() -> None:
    import yaml

    config = yaml.safe_load((ROOT / "aac.config.yaml").read_text())
    for d in config["declarations"]:
        assert d["rationale"].strip(), d["case"]
        if d["status"] == "accepted-risk":
            assert d["owner"] == "reference-agent" and d["review_by"], d["case"]
    named = {d["case"] for d in config["declarations"]}
    gaps = (ROOT / "evals" / "NOT_EXERCISED.md").read_text()
    assert all(f"### {case}" in gaps for case in named), (
        "a declaration NOT_EXERCISED.md does not explain"
    )


SUMMARY = {
    "applicable": 3,
    "covered": 1,
    "failing": 0,
    "not_covered": 1,
    "accepted_risk": 1,
    "not_applicable": 0,
}

# (name, a result, what its row must say)
ROWS = [
    (
        "a covered row names its outcome",
        {"case": "AAC-0001", "status": "covered", "outcome": "pass", "mechanisms": ["M1"]},
        "| AAC-0001 | pass | M1 |",
    ),
    (
        "an accepted risk shows its rationale",
        {"case": "AAC-0016", "status": "accepted-risk", "rationale": "by hand"},
        "| AAC-0016 |  |  | by hand |",
    ),
    (
        "a pipe in a note does not break the table",
        {"case": "AAC-0002", "status": "not-covered", "note": "a | b"},
        "| AAC-0002 |  |  | a / b |",
    ),
]


@pytest.mark.parametrize(("name", "result", "row"), ROWS, ids=[r[0] for r in ROWS])
def test_the_page_lists_every_obligation_by_status(name: str, result: dict, row: str) -> None:
    report = {
        "subject": {"name": "s", "version": "v", "archetypes": ["A6"]},
        "catalog_version": "0.16.0",
        "generated_at": "2026-10-01T00:00:00Z",
        "summary": SUMMARY,
        "results": [result],
    }
    page = aac_report.render(report, "", "tests", "scores.json", "a fixture")
    assert row in page
    assert "| 3 | 1 | 0 | 1 | 1 | 0 |" in page
    assert "  a fixture" in page
