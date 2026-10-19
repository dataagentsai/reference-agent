"""The `cost_ledger` port's conformance cases, one per invariant key (T-048, tier 2).

As `policy.conformance`: a case names what it checks as `cost_ledger/<key>`,
the key AHC's `ports/cost_ledger.yaml` gives the invariant, and takes a `Make`
that returns the realization under test, given the prices to charge. The file
is pinned by digest in the tests; a case edited so an adapter passes is a
failure of the adapter.

The two static invariants are checked by a run as well where one can show
them: usage cannot be recorded without a unit, and tags come back as given.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import jsonschema

from agent_harness.contracts import Usage
from agent_harness.cost import Price
from agent_harness.cost.port import SPENT_SCHEMA, CostLedger, Observation, total

Make = Callable[[dict[str, Price]], CostLedger]
"""Build the realization under test, charging these prices."""

MODEL = "conformance/model"
PRICES = {MODEL: Price(Decimal("1"), Decimal("2"))}
"""One dollar per million input tokens and two per million output: round numbers
so an expected total is arithmetic, not a second implementation of pricing."""
CALL = Usage(input_tokens=1_000_000, output_tokens=500_000)
"""Costs exactly two dollars at `PRICES`."""


@dataclass(frozen=True)
class Case:
    key: str
    """`cost_ledger/<invariant key>`."""
    capability: str
    """The AHC capability the invariant serves, as the port file gives it."""
    name: str
    check: Callable[[Make], None]


def _valid(ledger: CostLedger, unit: str) -> None:
    jsonschema.validate(total(ledger.spent(unit)), SPENT_SCHEMA)


def _a_retry_is_an_attempt_on_the_same_unit(make: Make) -> None:
    ledger = make(PRICES)
    for attempt in (1, 2, 3):
        ledger.record(Observation("run-1", MODEL, CALL, attempt=attempt))
    spent = ledger.spent("run-1")
    assert (spent.calls, spent.retries, spent.usd) == (3, 2, Decimal(6)), spent
    assert ledger.spent("run-2").calls == 0, "a retry became a unit of its own"
    _valid(ledger, "run-1")


def _units_do_not_share_a_total(make: Make) -> None:
    ledger = make(PRICES)
    ledger.record(Observation("run-1", MODEL, CALL))
    ledger.record(Observation("run-2", MODEL, CALL))
    assert ledger.spent("run-1").usd == ledger.spent("run-2").usd == Decimal(2)


def _the_total_is_readable_mid_run(make: Make) -> None:
    ledger = make(PRICES)
    seen = []
    for _ in range(3):
        ledger.record(Observation("run-1", MODEL, CALL))
        seen.append(ledger.spent("run-1").usd)
    assert seen == [Decimal(2), Decimal(4), Decimal(6)], seen
    _valid(ledger, "run-1")


def _an_unknown_unit_has_spent_nothing(make: Make) -> None:
    spent = make(PRICES).spent("never-seen")
    assert (spent.calls, spent.usd) == (0, Decimal(0)), spent


def _tags_come_back_as_given(make: Make) -> None:
    ledger = make(PRICES)
    tags = {"tenant": "T-1", "feature": "refunds", "route": "model"}
    ledger.record(Observation("run-1", MODEL, CALL, tags=tags))
    ledger.record(Observation("run-1", MODEL, CALL, attempt=2, tags={"tenant": "other"}))
    assert dict(ledger.spent("run-1").tags) == tags
    _valid(ledger, "run-1")


CASES: tuple[Case, ...] = (
    Case(
        "cost_ledger/retry_is_same_unit",
        "AHC-0024",
        "a retry is another attempt on the same unit, counted as a retry",
        _a_retry_is_an_attempt_on_the_same_unit,
    ),
    Case(
        "cost_ledger/attributed_to_unit",
        "AHC-0007",
        "two units never share a running total",
        _units_do_not_share_a_total,
    ),
    Case(
        "cost_ledger/total_readable_in_flight",
        "AHC-0030",
        "the running total is current after every observation",
        _the_total_is_readable_mid_run,
    ),
    Case(
        "cost_ledger/total_readable_in_flight",
        "AHC-0030",
        "a unit nothing was recorded on has spent nothing, and says so",
        _an_unknown_unit_has_spent_nothing,
    ),
    Case(
        "cost_ledger/boundary_tags_kept",
        "AHC-0090",
        "tags are the boundary's, never re-derived by a later observation",
        _tags_come_back_as_given,
    ),
)


__all__ = ["CALL", "CASES", "MODEL", "PRICES", "Case", "Make"]
