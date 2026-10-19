"""The `policy` port's conformance cases, run against our own realization (T-048).

The same table runs against every framework's adapter when its cycle builds one;
here it holds `RulePolicy`, the realization our loop's `enforce` sits under. The
case file is pinned by digest, so a case cannot be edited to let an adapter pass
without this test saying so.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from agent_harness.policy import Rule
from agent_harness.policy import conformance as policy_cases
from agent_harness.policy.port import Policy, RulePolicy
from agent_harness.policy.verdicts import Position

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-harness-catalog"
needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")

DIGEST = "1861994733a817ae8a965c2ed03c277beb01479a1b55fb1ec4ce9564d7dd81f8"
"""sha256 of `policy/conformance.py`. Changed only on purpose, with the reason in
the commit: the baseline T-048's builds are measured against."""


def _ours(rules: Mapping[Position, Sequence[Rule]], budget_s: float | None) -> Policy:
    return RulePolicy(rules, budget_s=budget_s)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, marks=pytest.mark.discharges(case.capability), id=case.name)
        for case in policy_cases.CASES
    ],
)
def test_the_reference_realization_conforms(case: policy_cases.Case) -> None:
    case.check(_ours)


@pytest.mark.tooling
def test_the_cases_are_the_pinned_ones() -> None:
    assert policy_cases.__file__ is not None
    digest = hashlib.sha256(Path(policy_cases.__file__).read_bytes()).hexdigest()
    assert digest == DIGEST, "the policy conformance cases changed: re-pin on purpose"


@needs_catalog
@pytest.mark.tooling
def test_every_dynamic_invariant_has_a_case() -> None:
    port = yaml.safe_load((CATALOG / "ports" / "policy.yaml").read_text())
    dynamic = {f"policy/{i['key']}" for i in port["invariants"] if i["checkable"] == "dynamic"}
    assert dynamic <= {case.key for case in policy_cases.CASES}
