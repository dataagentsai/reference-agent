"""The `cost_ledger` port's conformance cases, run against our own realization (T-048).

The same table runs against every framework's adapter when its cycle builds one;
here it holds `InMemoryLedger`, the ledger behind our loop's per-run `Meter`. The
case file is pinned by digest, so a case cannot be edited to let an adapter pass
without this test saying so.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agent_harness.cost import Price
from agent_harness.cost import conformance as cost_cases
from agent_harness.cost.port import CostLedger, InMemoryLedger

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-harness-catalog"
needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")

DIGEST = "58a827f0bc643aea256552ab9b72ca0d07004fc6ea62ace9e6d06ad44d5577b0"
"""sha256 of `cost/conformance.py`. Changed only on purpose, with the reason in
the commit: the baseline T-048's builds are measured against."""


def _ours(prices: dict[str, Price]) -> CostLedger:
    return InMemoryLedger(prices)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, marks=pytest.mark.discharges(case.capability), id=case.name)
        for case in cost_cases.CASES
    ],
)
def test_the_reference_realization_conforms(case: cost_cases.Case) -> None:
    case.check(_ours)


@pytest.mark.tooling
def test_the_cases_are_the_pinned_ones() -> None:
    assert cost_cases.__file__ is not None
    digest = hashlib.sha256(Path(cost_cases.__file__).read_bytes()).hexdigest()
    assert digest == DIGEST, "the cost_ledger conformance cases changed: re-pin on purpose"


@needs_catalog
@pytest.mark.tooling
def test_every_dynamic_invariant_has_a_case() -> None:
    port = yaml.safe_load((CATALOG / "ports" / "cost_ledger.yaml").read_text())
    dynamic = {f"cost_ledger/{i['key']}" for i in port["invariants"] if i["checkable"] == "dynamic"}
    assert dynamic <= {case.key for case in cost_cases.CASES}
