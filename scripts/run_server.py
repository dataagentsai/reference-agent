"""Run the agent behind HTTP, against a simulated shop.

    uv run python scripts/run_server.py           # mock model, no network, free
    uv run python scripts/run_server.py --real    # a real provider call per turn

Then open the printed URL. The token is in it — the page cannot mint one, so a
link without it is a page that can talk to nothing.

## Why the default is the simulated shop

The world comes from `worlds/clothing.yaml`, projected into a real MCP server in
this process. The agent cannot tell it from a production one: same protocol,
same schemas, same two error channels. So this runs with no ecommerce backend,
no credentials, and no way to charge anybody — while exercising the identical
code path a deployment would.

`--real` swaps only the model. Everything else stays simulated, which is the
resolution seam doing its job: one exit at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time

import uvicorn
from agenttwin import Live, load, project

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.config import Settings
from support_agent.contracts import ModelResponse
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import GroqClient, ScriptedClient
from support_agent.state import FileCheckpointStore
from support_agent.tools import connect

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.path.join(HERE, "worlds", "clothing.yaml")

SECRET = os.environ.get("AGENT_SESSION_SECRET", "dev-only-secret-not-for-production-32b!")
"""Overridable, and the default is loud about what it is. A 32-byte minimum is
enforced by `identity`, so a short one fails at startup rather than at the first
forged token."""


def demo_replies() -> ScriptedClient:
    """Enough canned turns to try the page. `--real` replaces this."""
    return ScriptedClient(
        [
            ModelResponse(text=t)
            for t in [
                "I can help with that. Could you give me the order number?",
                "Thank you — let me look that up.",
                "That is everything I can do on this one.",
            ]
        ]
        * 40
    )


async def main(real: bool, port: int) -> None:
    tel.configure()
    world = Live.start(load(WORLD))
    settings = Settings() if real else None

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        llm = (
            GroqClient(
                api_key=settings.provider_api_key,
                base_url=settings.provider_base_url,
                model=settings.model,
            )
            if real and settings is not None
            else demo_replies()
        )
        escalations = esc.InMemoryEscalationStore()
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=FileCheckpointStore(os.path.join(HERE, ".state")),
            approvals=ap.InMemoryApprovalStore(),
            escalations=escalations,
            # A measured desk, so the demo shows a real wait rather than a
            # promise. Twelve an hour is invented for the demo and would be
            # measured in a deployment — the point is that the number comes from
            # somewhere rather than from the reply text.
            capacity=esc.Capacity(per_hour=12),
            deliveries=trg.InMemoryDeliveryLog(),
        )
        app = serve.build(agent, secret=SECRET, escalations=escalations)

        token = ident.mint("C-1042", secret=SECRET, ttl_s=8 * 3600, now=int(time.time()))
        print("\n  Support agent running against the simulated clothing shop")
        print(f"  model: {'REAL — ' + settings.model if real else 'scripted (free, offline)'}")
        print("\n  Open this — the token is in the link:\n")
        print(f"    http://127.0.0.1:{port}/?token={token}\n")
        print("  Orders: AB-10001 shipped · AB-10002 pending · AB-10003 delivered 5d")
        print("          AB-10004 delivered 31d · AB-10005 final sale · AB-66666 poisoned note\n")

        # The sweeper, on a timer. `esc.sweep` owns no scheduling of its own so
        # a scenario can drive it; this is the deployment's half of that split.
        async def sweeping() -> None:
            while True:
                await asyncio.sleep(60)
                for lapsed in await esc.sweep(escalations):
                    print(f"  escalation {lapsed.id} lapsed — nobody came")

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        async with asyncio.TaskGroup() as group:
            sweeper = group.create_task(sweeping())
            await uvicorn.Server(config).serve()
            sweeper.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="use a real model provider")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.real, args.port))
    sys.exit(0)
