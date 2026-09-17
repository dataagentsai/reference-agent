"""T-028's done-when, against the composed Temporal rather than a test server.

    docker compose --profile durable up -d temporal

The offline tests run the real engine in memory, which proves the workflow and
not the *durability*: a server that keeps its state in a process cannot show
that a wait survives that process dying. This one restarts the container the
approval is waiting in, and then a colleague grants it.

Skipped when nothing answers at `AGENT_TEMPORAL_ADDRESS`, so the suite stays
runnable with no Docker.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel
from temporalio.client import Client

from support_agent import approvals as ap
from support_agent import identity as ident
from support_agent.contracts import ApprovalState, IdempotencyKey, Identity, RunId, SideEffectClass
from support_agent.idempotency import InMemoryLedger
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT, connect

ADDRESS = os.environ.get("AGENT_TEMPORAL_ADDRESS", "localhost:7233")
CUSTOMER = "C-1042"
ORDER = "AB-501"


class RefundOut(BaseModel):
    refund_id: str


class OrderOut(BaseModel):
    order_id: str
    status: str
    total: int


def shop() -> MCPServer:
    """One large, owed refund: the only shape that needs a person."""
    server = MCPServer("ecom")
    server.state = {"refunds": 0}  # type: ignore[attr-defined]

    @server.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="returned", total=24000)

    @server.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_REFUNDS_WRITE,
        }
    )
    def issue_refund(order_id: str) -> RefundOut:
        """Refund an order, for its total. Irreversible."""
        server.state["refunds"] += 1  # type: ignore[attr-defined]
        return RefundOut(refund_id=f"rf_{server.state['refunds']}")  # type: ignore[attr-defined]

    return server


async def client(*, wait_s: int = 0) -> Client:
    """Connect, or skip. After a restart the server takes a few seconds to
    answer again, so `wait_s` retries rather than calling that a failure."""
    deadline = time.time() + wait_s
    while True:
        try:
            return await asyncio.wait_for(ap.connect_temporal(ADDRESS), timeout=10)
        except Exception as exc:  # noqa: BLE001 — any failure to reach it is a skip
            if time.time() >= deadline:
                pytest.skip(f"no Temporal at {ADDRESS}: {exc}")
            await asyncio.sleep(2)


async def acting_for(customer_id: str) -> Identity:
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


def restart_the_server() -> None:
    """Stop and start the container the approval is waiting in."""
    if shutil.which("docker") is None:
        pytest.skip("no docker to restart the server with")
    done = subprocess.run(
        ["docker", "compose", "--profile", "durable", "restart", "temporal"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"could not restart the composed Temporal: {done.stderr[-200:]}")


@pytest.mark.discharges("AHC-0057", "P-APPROVAL-WAIT", "ext:approval_queue", "op:issue_refund")
async def test_an_approval_survives_the_server_it_is_waiting_in() -> None:
    """The wait outlives the process holding it, which is the whole of P6.

    A refund above the limit is raised and left with a colleague. The Temporal
    container is restarted — everything holding that wait in memory is gone —
    and afterwards the same approval is still there, is granted, and produces
    exactly one refund.
    """
    server = shop()
    queue = f"approvals-live-{int(time.time())}"
    key = IdempotencyKey(run_id=RunId(f"run_live_{int(time.time())}"), step=0, iteration=0)
    connected = await client()

    async with connect(server, ledger=InMemoryLedger()) as tools:
        work = ap.RefundWork(tools, acting_for=acting_for)
        activities = [work.assess, work.carry_out]
        approvals = ap.TemporalApprovals(connected, task_queue=queue)
        async with ap.worker(connected, activities=activities, task_queue=queue):
            raised = await approvals.request(
                action=ap.REFUND_ACTION,
                args={"order_id": ORDER},
                identity=Identity(customer_id=CUSTOMER, scopes=ident.CUSTOMER_SCOPES),
                idempotency_key=key,
            )
        assert raised.state is ApprovalState.WAITING
        assert server.state["refunds"] == 0

        restart_the_server()

        reborn = await client(wait_s=90)
        again = ap.TemporalApprovals(reborn, task_queue=queue)
        desk = ap.ApprovalDesk(reborn)
        async with ap.worker(reborn, activities=activities, task_queue=queue):
            waiting = await again.get(raised.id)
            assert waiting is not None, "the wait was lost with the server"
            assert waiting.state is ApprovalState.WAITING
            assert [a.id for a in await again.pending()] == [raised.id], "still in the queue"

            granted = await desk.decide(raised.id, granted=True, by="desk-1")

    assert granted.state is ApprovalState.DONE
    assert server.state["refunds"] == 1
    assert again.durable, "a composed Temporal keeps its state, and says so"
