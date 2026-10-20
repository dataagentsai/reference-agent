"""The `approval` port's conformance cases, run against both our waits (T-048).

Each rig is built through the composition, as `adapters.compose` builds what a
deployment binds: `temporal-updates` (the Open Stack) and `dbos-workflows`
(Azure). A framework's interrupt or permission hook gets a rig of its own when
its cycle builds one. The case file is pinned by digest.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("dbos", reason="the Azure extra (agent-harness[azure]) is not installed")

from temporalio import activity  # noqa: E402

from agent_harness.adapters import ADAPTERS, Adapter, Wiring  # noqa: E402
from agent_harness.approvals import conformance as approval_cases  # noqa: E402
from agent_harness.approvals.durable import (  # noqa: E402
    ASSESS,
    CARRY_OUT,
    Ask,
    Assessment,
    CarriedOut,
)
from agent_harness.approvals.workflow import Terms  # noqa: E402
from agent_harness.contracts import Approval, IdempotencyKey, Identity, RunId  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-harness-catalog"
needs_catalog = pytest.mark.skipif(not CATALOG.is_dir(), reason="the catalog is a sibling checkout")

DIGEST = "a6b592806579740586f34366f34a4ae97c84403011d5d0a761008831d41a1fd6"
"""sha256 of `approvals/conformance.py`. Changed only on purpose, with the
reason in the commit: the baseline T-048's builds are measured against."""

ENGINES = ["temporal-updates", "dbos-workflows"]


class Steps:
    """The agent's side of the wait: an assessment that needs a person and says
    what it judged, and a far end that counts what it carried out."""

    def __init__(self) -> None:
        self.carried = 0

    @activity.defn(name=ASSESS)
    async def assess(self, ask: Ask) -> Assessment:
        return Assessment(
            args=dict(ask.args),
            reason="above the automatic limit",
            approver="policy",
            decided_against=dict(approval_cases.JUDGED),
        )

    @activity.defn(name=CARRY_OUT)
    async def carry_out(self, approval: Approval) -> CarriedOut:
        self.carried += 1
        return CarriedOut(ok=True, text="carried out")


class NoTools:
    """The tool port's connection for the worker, which these steps never use."""

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        yield None


class ComposedRig:
    """One engine's waits, built by the composition and rebuilt on restart."""

    def __init__(self, engine: str, settings: dict[str, Any], hooks: dict[str, Any]) -> None:
        self.engine, self.settings, self.hooks = engine, settings, hooks
        self.steps = Steps()
        self.stack = AsyncExitStack()
        self.asked = 0

    async def open(self) -> None:
        chosen: Adapter = ADAPTERS.load("approval", self.engine)
        filled = {k: self.settings.get(k, s.default) for k, s in chosen.settings.items()}
        wiring = Wiring(
            filled,
            {"tool_runtime": NoTools()},
            {"work": lambda _tools: self.steps, **self.hooks},
            "test",
        )
        made = await self.stack.enter_async_context(chosen.build(wiring))
        self.agent, self.desk = made.approvals, made.approver

    @property
    def carried(self) -> int:
        return self.steps.carried

    @property
    def survives_restart(self) -> bool:
        return bool(getattr(self.agent, "durable", False))

    async def ask(self, *, ttl_s: int) -> Approval:
        self.asked += 1
        handle = dataclasses.replace(self.agent, policy=Terms(ttl_s=ttl_s, remind_before_s=0))
        return await handle.request(
            action="issue_refund",
            args={"order_id": f"AB-{self.asked}"},
            identity=Identity(customer_id="C-1042"),
            idempotency_key=IdempotencyKey(run_id=RunId("run_port"), step=self.asked, iteration=1),
        )

    async def restart(self) -> None:
        await self.stack.aclose()
        self.stack = AsyncExitStack()
        await self.open()


async def _conforms(engine: str, postgres: str, case: approval_cases.Case) -> None:
    async with AsyncExitStack() as outer:
        if engine == "temporal-updates":
            from temporalio.contrib.pydantic import pydantic_data_converter
            from temporalio.testing import WorkflowEnvironment

            env = await outer.enter_async_context(
                await WorkflowEnvironment.start_time_skipping(
                    data_converter=pydantic_data_converter
                )
            )
            settings = {"task_queue": f"port-{uuid.uuid4().hex[:8]}", "durable": False}
            rig = ComposedRig(engine, settings, {"client": env.client})
        else:
            url = f"{postgres}/port_{uuid.uuid4().hex[:10]}"
            rig = ComposedRig(engine, {"url": url, "name": "port-test"}, {})
        await rig.open()
        try:
            await case.check(rig)
        finally:
            await rig.stack.aclose()


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, marks=pytest.mark.discharges(case.capability), id=case.name)
        for case in approval_cases.CASES
    ],
)
def test_the_wait_conforms(postgres: str, engine: str, case: approval_cases.Case) -> None:
    asyncio.run(_conforms(engine, postgres, case))


@pytest.mark.tooling
def test_the_cases_are_the_pinned_ones() -> None:
    assert approval_cases.__file__ is not None
    digest = hashlib.sha256(Path(approval_cases.__file__).read_bytes()).hexdigest()
    assert digest == DIGEST, "the approval conformance cases changed: re-pin on purpose"


@needs_catalog
@pytest.mark.tooling
def test_every_dynamic_invariant_has_a_case() -> None:
    port = yaml.safe_load((CATALOG / "ports" / "approval.yaml").read_text())
    dynamic = {f"approval/{i['key']}" for i in port["invariants"] if i["checkable"] == "dynamic"}
    assert dynamic <= {case.key for case in approval_cases.CASES}
