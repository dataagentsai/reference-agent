"""B1 — the first real model call.

Everything in the test suite is scripted or replayed. This is the one place a
live provider is reached, and it exists to find out what scripted tests cannot
tell you: whether the model follows the instructions, whether tool arguments
survive the round trip, and what a turn actually costs.

It runs behind a `Recorder`, so one live call becomes a permanent offline
cassette rather than a one-off. Run it, then run `--replay` to prove the
recording reproduces the run with no network at all.

    uv run python scripts/first_real_call.py           # live; --record to overwrite
    uv run python scripts/first_real_call.py --replay   # offline, from disk

The key is read from AGENT_PROVIDER_API_KEY and never printed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.cassette import Cassette, Player, Recorder
from support_agent.config import Settings, resolve
from support_agent.contracts import Identity, SideEffectClass
from support_agent.cost import Meter
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import GroqClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import META_SIDE_EFFECT, connect

CASSETTE = Path(__file__).parent.parent / "cassettes" / "first_real_call.json"
QUESTION = "Hi — can you tell me what is happening with my order AB-77120? I ordered it last week."


class OrderOut(BaseModel):
    order_id: str
    status: str
    carrier: str


def build_server() -> MCPServer:
    srv = MCPServer("ecom")

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up the current status of a customer's order by its id."""
        return OrderOut(order_id=order_id, status="out_for_delivery", carrier="Delhivery")

    return srv


async def main(replay: bool, record: bool = False) -> int:
    tel.configure()
    settings = Settings(provider_api_key=os.environ.get("AGENT_PROVIDER_API_KEY", ""))
    if not replay and not settings.provider_api_key:
        print("AGENT_PROVIDER_API_KEY is not set", file=sys.stderr)
        return 2

    config = resolve(settings)
    meter = Meter(config.model, ceiling_usd=config.budgets.max_cost_usd)

    if replay:
        llm: object = Player(Cassette.load(CASSETTE))
        recorder = None
    else:
        recorder = Recorder(
            GroqClient(
                api_key=settings.provider_api_key,
                base_url=config.provider_base_url,
                model=config.model,
                temperature=config.temperature,
            )
        )
        llm = recorder

    who: Identity = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)

    async with connect(build_server(), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=llm,  # type: ignore[arg-type]
            tools=tools,
            store=InMemoryCheckpointStore(),
            config=config,
        )
        result, _ = await agent.handle(QUESTION, identity=who)

    print(f"  mode      {'replay (no network)' if replay else 'live'}")
    print(f"  model     {config.model}")
    print(f"  config    {config.fingerprint}")
    print(f"  result    {result.kind}")
    print(f"  reply     {getattr(result, 'reply', getattr(result, 'customer_message', ''))!r}")
    detail = getattr(result, "detail", "")
    if detail:
        print(f"  detail    {detail[:600]}")

    if recorder is not None:
        for exchange in recorder.cassette:
            meter.record(exchange.response.usage)
        print(f"  calls     {meter.calls}")
        print(f"  cost      ${meter.spend:.6f}")
        # The committed recording is a test fixture, and a live run is not a
        # reason to replace it: running this script once quietly overwrote it and
        # a release gate failed on a recording nobody meant to change.
        if CASSETTE.exists() and not record:
            print(f"  kept      {CASSETTE.name} unchanged — pass --record to replace it")
        else:
            recorder.cassette.save(CASSETTE)
            where = CASSETTE.relative_to(Path.cwd())
            print(f"  recorded  {where} ({len(recorder.cassette)} exchanges)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--replay" in sys.argv, "--record" in sys.argv)))
