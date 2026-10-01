"""The instrument that says which tests verify which statements.

Tooling: these test the map, not the agent — so they carry `tooling`, and they
stay out of the untagged remainder they help compute.
"""

from __future__ import annotations

import pytest
from evals import assurance_map as amap
from evals import statements

pytestmark = pytest.mark.tooling

VOCAB = statements.load()

FAMILIES = [
    ("AAC-0047", "AAC"),
    ("AHC-0074", "AHC"),
    ("B5", "Baseline"),
    ("P-CANCEL", "AOAS"),
    ("op:cancel_order", "AOAS"),
    ("esc:loop-exhausted", "AOAS"),
]


@pytest.mark.parametrize(("sid", "fam"), FAMILIES, ids=[f[0] for f in FAMILIES])
def test_an_id_is_placed_by_its_shape(sid: str, fam: str) -> None:
    assert statements.family(sid) == fam


KNOWN = [
    ("an AOAS policy", "P-OWNERSHIP", True),
    ("an AOAS operation", "op:issue_refund", True),
    ("an AOAS escalation rule", "esc:second-refusal", True),
    ("an AOAS external contract", "ext:order_system", True),
    ("an A6 obligation", "AAC-0055", True),
    ("a harness capability", "AHC-0074", True),
    ("a baseline item", "B12", True),
    ("a policy that does not exist", "P-TELEPORT", False),
    ("an operation that does not exist", "op:teleport", False),
    ("an obligation past the end", "AAC-9999", False),
    ("a capability past the end", "AHC-9999", False),
    ("a baseline item past the end", "B99", False),
]


@pytest.mark.parametrize(("name", "sid", "known"), KNOWN, ids=[k[0] for k in KNOWN])
def test_an_unknown_statement_is_refused(name: str, sid: str, known: bool) -> None:
    assert (VOCAB.unknown((sid,)) == []) is known


def test_the_unit_is_the_function_and_the_remainders_are_both_directions() -> None:
    outcomes = [
        amap.Outcome("t.py::test_a[1]", ("P-CANCEL",), passed=True, tooling=False),
        amap.Outcome("t.py::test_a[2]", ("P-CANCEL",), passed=False, tooling=False),
        amap.Outcome("t.py::test_b", (), passed=True, tooling=False),
        amap.Outcome("t.py::test_c", (), passed=True, tooling=True),
    ]
    m = amap.build(outcomes, VOCAB)
    assert (m["functions"], m["tagged"]) == (3, 1), "cases of one function count once"
    assert m["untagged"] == ["t.py::test_b"], "tooling stays out of the remainder"
    assert "P-CANCEL" in m["families"]["AOAS"]["failing"], "one failing case fails the function"
    assert any(s["id"] == "P-RETURN" for s in m["families"]["AOAS"]["not_exercised"])


def test_a_statement_verified_only_on_dead_code_is_not_met() -> None:
    outcomes = [
        amap.Outcome("t.py::test_breaker", ("AHC-0005",), passed=True, tooling=False, unwired=True),
    ]
    m = amap.build(outcomes, VOCAB)
    assert m["tagged"] == 0 and m["untagged"] == []
    assert m["unwired"] == {"t.py::test_breaker": ["AHC-0005"]}
    assert "AHC-0005" not in m["by_statement"], "an unwired test exercises nothing"


# (why, the cases of one function, where its statement lands) — F-073: a test
# that skipped in its body was recorded as passed=False, so the map listed
# test_a_refused_call_does_not_put_the_gateway_on_cooldown as failing AHC-0005.
SKIPS = [
    ("skipped alone is not exercised", [None], "not_exercised"),
    ("skipped beside a pass is a pass", [None, True], "passed"),
    ("skipped beside a failure is a failure", [None, False], "failed"),
]


@pytest.mark.parametrize(("why", "cases", "lands"), SKIPS, ids=[s[0] for s in SKIPS])
def test_a_skipped_case_is_not_evidence_either_way(why: str, cases: list, lands: str) -> None:
    outcomes = [
        amap.Outcome(
            f"t.py::test_gateway[{i}]",
            ("AHC-0005",),
            passed=bool(ran),
            tooling=False,
            skipped=ran is None,
        )
        for i, ran in enumerate(cases)
    ]
    m = amap.build(outcomes, VOCAB)
    held = m["by_statement"].get("AHC-0005", {"passed": [], "failed": []})
    landed = "passed" if held["passed"] else "failed" if held["failed"] else "not_exercised"
    assert landed == lands, held
