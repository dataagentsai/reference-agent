"""The `cost_ledger` port, named as AHC names it: `record` and `spent` (T-048).

`Meter` is the per-run budget our loop checks between calls. This is the ledger
behind it, with the unit of work explicit rather than implied by which object
was handed in, so a loop we do not own (a LangGraph run, a Claude Agent SDK
session) records into the same thing and is held to the same cases
(`cost.conformance`).

    record   one usage observation on a unit of work, with its attempt number
             and the tags the boundary gave it; returns what it cost
    spent    the unit's running total, readable while the unit is still running

`reserve` is optional in the port and not offered: no stack we run spends in
parallel within one unit yet.

**A retry is an attempt, not a unit** (`cost_ledger/retry_is_same_unit`). The
caller says which attempt an observation belongs to; the ledger counts it on the
same unit and keeps the count of retries, so the cost of unreliability is a
number rather than an inflated volume of healthy-looking calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from agent_harness.contracts import Usage
from agent_harness.cost import Price, cost_of, price_of


@dataclass(frozen=True)
class Observation:
    """One model call's usage, as it crosses the port."""

    unit: str
    """The unit of work: a run, a task, a session. Never a call."""
    model: str
    usage: Usage
    attempt: int = 1
    """1 for the first try of a step, 2 for its first retry, and so on."""
    tags: Mapping[str, str] = field(default_factory=dict)
    """As the boundary applied them (tenant, feature, route); kept, never re-derived."""


@dataclass(frozen=True)
class Spent:
    """A unit's running total, as `spent` returns it."""

    unit: str
    usd: Decimal
    calls: int
    """Every observation, retries included."""
    retries: int
    """Observations with an attempt after the first."""
    tags: Mapping[str, str]
    """The tags of the unit's first observation, as given."""


class CostLedger(Protocol):
    """The port. A realization owns where totals live; prices are the deployment's."""

    def record(self, observation: Observation) -> Decimal: ...

    def spent(self, unit: str) -> Spent: ...


class InMemoryLedger:
    """The reference realization: totals per unit in this process.

    An unknown model raises `UnknownPrice` rather than pricing at zero (AHC-0101).
    """

    def __init__(self, prices: dict[str, Price] | None = None, *, keep: int = 10_000) -> None:
        self._prices = prices
        self._units: dict[str, Spent] = {}
        self.keep = keep
        """The most recent units held; the oldest is forgotten past it, so a
        long-running process holds a bounded window, not every turn it served."""

    def record(self, observation: Observation) -> Decimal:
        if observation.attempt < 1:
            raise ValueError(f"attempt must be 1 or more, not {observation.attempt}")
        amount = cost_of(observation.usage, price_of(observation.model, self._prices))
        was = self._units.pop(observation.unit, None)
        if was is None and len(self._units) >= self.keep:
            del self._units[next(iter(self._units))]
        self._units[observation.unit] = Spent(
            unit=observation.unit,
            usd=amount if was is None else was.usd + amount,
            calls=1 if was is None else was.calls + 1,
            retries=(0 if was is None else was.retries) + (observation.attempt > 1),
            tags=dict(observation.tags) if was is None else was.tags,
        )
        return amount

    def spent(self, unit: str) -> Spent:
        return self._units.get(unit) or Spent(unit, Decimal(0), 0, 0, {})


SPENT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "agent-harness/cost_ledger/spent",
    "title": "A unit of work's running total",
    "type": "object",
    "required": ["unit", "usd", "calls", "retries", "tags"],
    "additionalProperties": False,
    "properties": {
        "unit": {"type": "string", "minLength": 1},
        "usd": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+)?$"},
        "calls": {"type": "integer", "minimum": 0},
        "retries": {"type": "integer", "minimum": 0},
        "tags": {"type": "object", "additionalProperties": {"type": "string"}},
    },
}
"""Money crosses as a decimal string: a float would drift, as `Price` says."""


def total(spent: Spent) -> dict[str, object]:
    """A running total as it crosses the port and enters a record."""
    return {
        "unit": spent.unit,
        "usd": format(spent.usd, "f"),
        "calls": spent.calls,
        "retries": spent.retries,
        "tags": dict(spent.tags),
    }


__all__ = [
    "SPENT_SCHEMA",
    "CostLedger",
    "InMemoryLedger",
    "Observation",
    "Spent",
    "total",
]
