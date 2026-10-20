"""The `cost_ledger` port's conformance cases, run against our own realization (T-048).

The same table runs against every framework's adapter when its cycle builds one;
here it holds `InMemoryLedger`, the ledger behind our loop's per-run `Meter`. The
case file is pinned by digest, so a case cannot be edited to let an adapter pass
without this test saying so.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
import yaml

from agent_harness.adapters import ADAPTERS, Wiring
from agent_harness.contracts import Usage
from agent_harness.cost import Meter, Price
from agent_harness.cost import conformance as cost_cases
from agent_harness.cost.port import CostLedger, InMemoryLedger, Observation
from agent_harness.loop import spend

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-harness-catalog"
needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")

DIGEST = "58a827f0bc643aea256552ab9b72ca0d07004fc6ea62ace9e6d06ad44d5577b0"
"""sha256 of `cost/conformance.py`. Changed only on purpose, with the reason in
the commit: the baseline T-048's builds are measured against."""


def _ours(prices: dict[str, Price]) -> CostLedger:
    return InMemoryLedger(prices)


def _composed(prices: dict[str, Price]) -> CostLedger:
    """The ledger as the composition builds it from a profile's `per-task-meter`."""
    chosen = ADAPTERS.load("cost_ledger", "per-task-meter")
    built = chosen.build(Wiring({"keep": 10_000}, {}, {"prices": prices}, "test"))
    ledger: CostLedger = asyncio.run(built.__aenter__())
    return ledger


REALIZATIONS = {"in-memory": _ours, "per-task-meter, composed": _composed}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, marks=pytest.mark.discharges(case.capability), id=case.name)
        for case in cost_cases.CASES
    ],
)
@pytest.mark.parametrize("realization", REALIZATIONS)
def test_the_reference_realization_conforms(case: cost_cases.Case, realization: str) -> None:
    case.check(REALIZATIONS[realization])


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


@pytest.mark.parametrize(
    ("attempts", "calls", "retries"),
    [((1,), 1, 0), ((1, 2), 2, 1), ((1, 1, 2, 3), 4, 2)],
    ids=["one call", "one retry", "two steps, the second retried twice"],
)
@pytest.mark.discharges("AHC-0024", "AHC-0030")
def test_the_loops_meter_records_through_the_ledger_it_is_given(
    attempts: tuple[int, ...], calls: int, retries: int
) -> None:
    ledger = InMemoryLedger(cost_cases.PRICES)
    meter = Meter(
        cost_cases.MODEL, ceiling_usd=100, prices=cost_cases.PRICES, ledger=ledger, unit="run-1"
    )
    for attempt in attempts:
        meter.record(cost_cases.CALL, attempt=attempt)
    spent = ledger.spent("run-1")
    assert (spent.calls, spent.retries) == (calls, retries)
    assert meter.spend == spent.usd == 2 * calls


# (keep, units recorded in order, units still held)
WINDOWS = [
    (3, ["a", "b", "c"], {"a", "b", "c"}),
    (2, ["a", "b", "c"], {"b", "c"}),
    (2, ["a", "b", "a", "c"], {"a", "c"}),
]


@pytest.mark.parametrize(
    ("keep", "recorded", "held"),
    WINDOWS,
    ids=["within the window", "the oldest is forgotten", "a unit used again is recent"],
)
@pytest.mark.discharges("AHC-0030")
def test_a_long_running_ledger_holds_a_bounded_window(
    keep: int, recorded: list[str], held: set[str]
) -> None:
    ledger = InMemoryLedger(cost_cases.PRICES, keep=keep)
    for unit in recorded:
        ledger.record(Observation(unit, cost_cases.MODEL, cost_cases.CALL))
    assert {u for u in "abc" if ledger.spent(u).calls} == held


@pytest.mark.parametrize(("attempt", "retries"), [(1, 0), (3, 1)], ids=["first try", "third try"])
@pytest.mark.discharges("AHC-0024")
def test_the_loops_account_carries_the_attempt_to_the_ledger(attempt: int, retries: int) -> None:
    ledger = InMemoryLedger(cost_cases.PRICES)
    meter = Meter(cost_cases.MODEL, ceiling_usd=100, prices=cost_cases.PRICES, ledger=ledger)
    spend.account(Usage(), cost_cases.CALL, meter=meter, attempt=attempt)
    assert ledger.spent(meter.unit).retries == retries
