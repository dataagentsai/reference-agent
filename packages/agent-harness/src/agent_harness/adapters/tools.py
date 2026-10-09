"""The `tool_runtime` port: the agent's tools, over MCP.

    mcp-client   an MCP server at `url`, or (the hook `server`) one in this
                 process, as a test runs the real adapter against it

Every call is claimed in the state port's ledger, and when the identity port has
an exchange, carries a token addressed to the far end. `connect` opens another
connection to the same server: an approval worker's own, as a deployment's
worker has its own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring
from agent_harness.contracts import ToolClient


@dataclass(frozen=True)
class Tools:
    client: ToolClient
    connect: Callable[[], AbstractAsyncContextManager[ToolClient]]


@asynccontextmanager
async def _mcp(wiring: Wiring) -> AsyncIterator[Tools]:
    from agent_harness.tools import connect

    target = wiring.hooks.get("server") or wiring.settings.get("url")
    if not target:
        raise OverlayRefused("mcp-client: give a `url`, or the hook `server`")
    sessions = wiring.built.get("identity")
    exchange = getattr(sessions, "exchange", None)
    ledger = wiring.built["state"].requests
    limit = int(wiring.settings["max_result_chars"])

    def another() -> AbstractAsyncContextManager[ToolClient]:
        return connect(target, requests=ledger, max_result_chars=limit, exchange=exchange)

    async with another() as client:
        yield Tools(client, another)


MCP_CLIENT = Adapter(
    "tool_runtime",
    "mcp-client",
    _mcp,
    {"url": Setting(), "max_result_chars": Setting(default=8000)},
)

__all__ = ["MCP_CLIENT", "Tools"]
