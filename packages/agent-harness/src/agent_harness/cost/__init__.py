"""What it costs and who pays.

L13 · P3 and P4. Two positions, deliberately.

**Attribution at P3** — every call is priced and tagged where it is made, so
spend can be sliced by tenant, feature and route afterwards (AAC-0104).

**The ceiling at P4** — only the control loop can see that fourteen calls are one
runaway task rather than fourteen tasks. A per-call limit cannot know that;
`max_output_tokens` is the per-call backstop, not the budget.

The ceiling is checked **between** calls, because you cannot un-spend one. A
single call can therefore overshoot it, bounded by `max_output_tokens`. That is
stated rather than hidden: a budget that claims to be exact and is not is worse
than one that says where its edge is.

AAC-0008 is cost per **successful task**, not per call. An agent that fails
cheaply four times and succeeds on the fifth was not cheap, and a per-call metric
reports it as though it were.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from agent_harness.contracts import Usage
from agent_harness.contracts.failures import AgentFailure, Fault

if TYPE_CHECKING:
    from agent_harness.cost.port import CostLedger

MTOK = Decimal(1_000_000)


class UnknownPrice(AgentFailure):
    """No price for this model.

    Raised rather than defaulted to zero. A missing price that prices at zero is
    a ceiling that never fires — the failure is silent, unbounded and only
    visible on an invoice.
    """

    fault = Fault.MISCONFIGURED


@dataclass(frozen=True)
class Price:
    """Per million tokens, in USD.

    `Decimal`, not `float`. Money that accumulates in binary floating point
    drifts, and a budget comparison that is wrong in the last place is a budget
    that fires late.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None = None
    """Cache reads are billed differently where a provider offers them. Left
    `None` where it does not, in which case cached tokens are billed as input."""


# Approximate, recorded 2026-09-01. These are an input to a budget, not a
# quotation — verify against the provider's pricing page before any figure from
# this system is published or acted on. Tracked as T-025 in
# clean-ai-engineering/TODO.md for that reason.
PRICES: dict[str, Price] = {
    "openai/gpt-oss-120b": Price(Decimal("0.15"), Decimal("0.75")),
    "openai/gpt-oss-20b": Price(Decimal("0.10"), Decimal("0.50")),
    "qwen/qwen3.8-27b": Price(Decimal("0.15"), Decimal("0.60")),
}


def price_of(model: str, prices: dict[str, Price] | None = None) -> Price:
    table = PRICES if prices is None else prices
    try:
        return table[model]
    except KeyError as exc:
        raise UnknownPrice(f"no price for model {model!r}") from exc


def cost_of(usage: Usage, price: Price) -> Decimal:
    """One call's cost."""
    cached_rate = (
        price.cached_input_per_mtok
        if price.cached_input_per_mtok is not None
        else price.input_per_mtok
    )
    return (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
        + Decimal(usage.cached_input_tokens) * cached_rate
    ) / MTOK


class Meter:
    """Accumulates spend across one run and answers whether the ceiling is past.

    Constructed with the price resolved, so an unknown model fails when the run
    is set up rather than three calls in.

    With a `cost_ledger` handed in (`cost.port`), every call is recorded there
    under `unit`, so the loop's spend goes through the same port, and is held to
    the same cases, as a framework's would. Without one the meter keeps its own
    tally; it never builds a ledger itself, because only the composition
    constructs a realization (B13).
    """

    def __init__(
        self,
        model: str,
        *,
        ceiling_usd: float | Decimal,
        prices: dict[str, Price] | None = None,
        ledger: CostLedger | None = None,
        unit: str | None = None,
    ) -> None:
        self.model = model
        self.price = price_of(model, prices)
        self.ceiling = Decimal(str(ceiling_usd))
        self.ledger = ledger
        self.unit = unit or f"meter-{uuid.uuid4().hex}"
        self._spend = Decimal(0)
        self._calls = 0

    def record(self, usage: Usage, *, attempt: int = 1) -> Decimal:
        """Price one call, add it to the unit, and return what it cost."""
        if self.ledger is not None:
            from agent_harness.cost.port import Observation

            observation = Observation(self.unit, self.model, usage, attempt=attempt)
            return self.ledger.record(observation)
        amount = cost_of(usage, self.price)
        self._spend += amount
        self._calls += 1
        return amount

    @property
    def spend(self) -> Decimal:
        return self._spend if self.ledger is None else self.ledger.spent(self.unit).usd

    @property
    def calls(self) -> int:
        return self._calls if self.ledger is None else self.ledger.spent(self.unit).calls

    @property
    def exceeded(self) -> bool:
        return self.spend >= self.ceiling

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.ceiling - self.spend)

    def per_successful_task(self, successes: int) -> Decimal | None:
        """AAC-0008. `None` when nothing succeeded — which is the honest answer,
        not infinity and certainly not the total."""
        if successes <= 0:
            return None
        return self.spend / Decimal(successes)

    def as_usd(self) -> float:
        """For a span attribute. Lossy on purpose — telemetry is for trends, the
        `Decimal` is for the decision."""
        return float(self.spend)


__all__ = [
    "PRICES",
    "Meter",
    "Price",
    "UnknownPrice",
    "cost_of",
    "price_of",
]
