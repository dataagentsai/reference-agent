"""The `cost_ledger` port: where a unit of work's spend is totalled.

    per-task-meter   in this process, per unit of work; the most recent `keep`
                     units are held, so a server that runs for weeks does not
                     grow without bound (every stack binds it today)

The agent's meter records each model call into it under the turn's unit, with
the attempt number, so the loop's spend goes through the same port and the same
conformance cases a framework's would (`cost.conformance`). Prices are the
deployment's: the hook `prices` replaces the library's table where given.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_harness.adapters import Adapter, Setting, Wiring
from agent_harness.cost.port import CostLedger


@asynccontextmanager
async def _in_process(wiring: Wiring) -> AsyncIterator[CostLedger]:
    from agent_harness.cost.port import InMemoryLedger

    yield InMemoryLedger(wiring.hooks.get("prices"), keep=int(wiring.settings["keep"]))


PER_TASK_METER = Adapter(
    "cost_ledger", "per-task-meter", _in_process, {"keep": Setting(default=10_000)}
)

__all__ = ["PER_TASK_METER"]
