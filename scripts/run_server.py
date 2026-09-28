"""Run the agent behind HTTP, against a simulated shop.

    uv run python scripts/run_server.py           # mock model, no network, free
    uv run python scripts/run_server.py --real    # a real provider call per turn
    uv run python scripts/run_server.py --store   # against the composed Saleor (T-017)

Then open the printed URL. The token is in it — the page cannot mint one, so a
link without it is a page that can talk to nothing. The token is signed by the
local test issuer in `evals/issuer.py`, because the agent can only verify
sessions, never sign them (T-002). Against Keycloak a token comes from the realm.

## Why the default is the simulated shop

The world comes from `worlds/clothing.yaml`, projected into a real MCP server in
this process. The agent cannot tell it from a production one: same protocol,
same schemas, same two error channels. So this runs with no ecommerce backend,
no credentials, and no way to charge anybody — while exercising the identical
code path a deployment would.

`--real` swaps only the model, and `--store` only the shop — one exit at a time,
which is what the resolution seam is for. With `--store` the agent talks to the
composed Saleor through the store's own MCP server, and every call is checked by
the far end rather than trusted.
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
from starlette.applications import Starlette
from starlette.routing import Mount
from temporalio.contrib.pydantic import pydantic_data_converter as _CONVERTER

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections.abc import AsyncIterator
from typing import Any

from evals import issuer as local_issuer  # noqa: E402

from support_agent import approvals as ap
from support_agent import channel as ch
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import portal as ptl
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.config import RunConfig, Settings, resolve
from support_agent.contracts import Approvals, Identity, LLMClient, ModelResponse, ToolClient
from support_agent.identity import APPROVER_SCOPES, REVIEWER_SCOPES, Exchange, sessions
from support_agent.identity.sessions import KeycloakLogin, KeycloakRefresh, Resume
from support_agent.llm import ScriptedClient, connect_model
from support_agent.requests import InMemoryRequests
from support_agent.resilience import ResilientLLM
from support_agent.state import InMemoryCheckpointStore, InMemorySessionStore
from support_agent.tools import connect

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.path.join(HERE, "worlds", "clothing.yaml")


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


def _telemetry(settings: Settings | None) -> None:
    """Install the provider, and point it at a collector if one is configured.

    The endpoint is read here rather than in `tel.configure` because it is a
    deployment's decision and the composition root is where those live. The
    in-memory exporter stays either way — it is what the eval harness asserts
    against, and a suite that went blind the moment a deployment gained a
    backend would be a suite that only works where nobody is watching.
    """
    configured = settings or Settings()
    tel.configure(
        capture_payloads=configured.capture_payloads,
        capture_sample=configured.capture_sample,
        environment=configured.deployment,
        metrics_endpoint=configured.metrics_endpoint or None,
    )
    if configured.deployment != "local" and not tel.exporting_metrics():
        # AHC-0111: a deployment whose numbers go nowhere is one nobody can be
        # told is broken, so it does not start.
        raise SystemExit(f"AGENT_DEPLOYMENT={configured.deployment} needs AGENT_METRICS_ENDPOINT")
    if configured.metrics_endpoint:
        print(f"  metrics going to {configured.metrics_endpoint}")
    if not configured.otlp_endpoint:
        return
    pairs = (p.split("=", 1) for p in configured.otlp_headers.split(",") if "=" in p)
    if tel.export_to(configured.otlp_endpoint, headers=dict(pairs)):
        print(f"  telemetry also going to {configured.otlp_endpoint}")


def _issuer() -> ident.Issuer:
    configured = Settings()
    if not configured.issuer_url:
        return local_issuer.issuer()
    keys = ident.RemoteJWKS.discover(configured.issuer_url)
    return ident.Issuer(url=configured.issuer_url, audience=configured.issuer_audience, keys=keys)


def _with_chatwoot(app: Starlette, agent: ep.Agent, issuer: ident.Issuer) -> Starlette:
    """Mount the customer portal at /portal and Chatwoot's webhook at /chatwoot
    when a realm, a cookie key and a Chatwoot inbox are configured (T-026).

    The two share one store of logins and one `Resume`, which is what makes
    logging out in the portal stop the agent acting for that customer.
    """
    configured = Settings()
    if not (
        configured.issuer_url and configured.portal_cookie_key and configured.chatwoot_base_url
    ):
        return app
    client = (configured.portal_client_id, configured.portal_client_secret)
    logins = InMemorySessionStore()
    resume = Resume(
        logins,
        KeycloakRefresh.discover(
            configured.issuer_url, client_id=client[0], client_secret=client[1]
        ),
        issuer=issuer,
    )
    portal = ptl.Portal(
        login=KeycloakLogin.discover(
            configured.issuer_url, client_id=client[0], client_secret=client[1]
        ),
        issuer=issuer,
        sessions=logins,
        widget=ptl.Widget(
            base_url=configured.chatwoot_base_url,
            website_token=configured.chatwoot_website_token,
            hmac_token=configured.chatwoot_hmac_token,
        ),
        redirect_uri=configured.portal_redirect_uri,
        base_path="/portal",
        cookie_key=configured.portal_cookie_key.encode(),
        secure_cookies=configured.portal_redirect_uri.startswith("https://"),
        forget=resume.forget,
    )
    channel = ch.Channel(
        agent=agent,
        store=agent.store,
        sessions=resume,
        api=ch.ChatwootClient(configured.chatwoot_base_url, token=configured.chatwoot_bot_token),
        portal_url=configured.portal_redirect_uri.rsplit("/", 1)[0] + "/",
        secret=configured.chatwoot_bot_secret,
    )
    print("  Customer portal at /portal; Chatwoot webhook at /chatwoot/webhook.")
    return Starlette(
        routes=[
            Mount("/portal", app=ptl.build(portal)),
            Mount("/chatwoot", app=ch.build(channel)),
            Mount("/", app=app),
        ]
    )


def _announce(port: int, settings: Settings | None, *, local: bool, store: bool = False) -> None:
    """The links a person needs, with tokens from the process-local issuer."""
    if not local:
        print(f"\n  Support agent on http://127.0.0.1:{port}, sessions from the realm.")
        print("  A token: POST the realm's token endpoint as client support-chat.\n")
        return
    token = local_issuer.mint("C-1042", ttl_s=8 * 3600)
    shop = "Saleor, the real store" if store else "the simulated clothing shop"
    print(f"\n  Support agent running against {shop}")
    print(
        f"  model: {'REAL — ' + settings.model if settings is not None else 'scripted (free, offline)'}"
    )
    print("\n  Open this — the token is in the link:\n")
    print(f"    http://127.0.0.1:{port}/?token={token}\n")
    # The desk at /ops needs a reviewer: a login with no customer behind it,
    # signed by the same process-local issuer, so it only works against this run.
    desk = local_issuer.mint(
        None, scopes=REVIEWER_SCOPES | APPROVER_SCOPES, subject="desk-1", ttl_s=8 * 3600
    )
    print(f"  Desk token for /ops — escalations and approvals (T-059):\n\n    {desk}\n")
    print(f"    the desk:  http://127.0.0.1:{port}/ops/desk?token={desk}")
    print(f"    the API:   http://127.0.0.1:{port}/ops/docs\n")
    print("  Orders: AB-10001 shipped · AB-10002 pending · AB-10003 delivered 5d")
    print("          AB-10004 delivered 31d · AB-10005 final sale · AB-66666 poisoned note\n")


async def _model(settings: Settings | None) -> tuple[LLMClient, RunConfig | None]:
    """The scripted demo, or the real provider behind `ResilientLLM` with its
    declared provider checked against the endpoint first (T-018)."""
    if settings is None:
        return demo_replies(), None
    config = resolve(settings)
    client, verified = await connect_model(config, api_key=settings.provider_api_key)
    print(f"  provider {config.provider}: {'verified' if verified else 'UNVERIFIED'}")
    return ResilientLLM(client), config


class LateTools:
    """The approvals worker's tool client, connected once the shop is up.

    The real store checks every call against the approvals, and the approvals
    worker acts on the store, so one of them has to be wired after the other. A
    deployment has no such knot: the worker is its own process with its own
    connection, which is the point of it (T-028). This is the demo's honest
    version of that.
    """

    def __init__(self) -> None:
        self.inner: ToolClient | None = None

    async def list_tools(self, identity: Identity) -> Any:
        assert self.inner is not None, "the shop is not up yet"
        return await self.inner.list_tools(identity)

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        assert self.inner is not None, "the shop is not up yet"
        return await self.inner.call(*args, **kwargs)


@contextlib.asynccontextmanager
async def shop_running(
    real_store: bool, issuer: ident.Issuer, approvals: Approvals
) -> AsyncIterator[tuple[ToolClient, ToolClient]]:
    """The tools, from the simulated shop or the real one (T-017).

    Two clients over one shop: the **agent's**, whose calls carry the customer's
    session exchanged for the store, and the **approvals worker's**, which has no
    session to exchange because a refund is carried out an hour after the
    customer left. The worker logs in as itself, and the far end reads whose the
    call is from the approval it names (T-028).

    One client for both worked until the agent met a realm: the worker's
    identity carries no token, the realm's exchange has nothing to exchange, and
    every refund that needed a person failed on the read that assesses it.

    The only difference the agent can see is which MCP server answers. The real
    store checks every call itself — the token addressed to it, and the approval
    for anything the token does not already allow — so the far end here is a far
    end rather than a courtesy.
    """
    if not real_store:
        async with connect(project(Live.start(load(WORLD))), requests=InMemoryRequests()) as tools:
            print("  shop           the simulated clothing shop, projected from worlds/")
            yield tools, tools
        return

    from order_system import server as store_server
    from order_system.store import Saleor, Store

    url = os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")
    shop = Store(
        Saleor(
            url,
            os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com"),
            os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only"),
        )
    )
    server = store_server.build(
        shop,
        issuer=ident.Issuer(url=issuer.url, audience="order-system", keys=issuer.keys),
        clock=lambda: int(time.time()),
        approvals=approvals,
        # Which client the approvals worker logs in as, so the far end knows
        # whose party token to take a customer from. It is the same name the
        # worker signs in under; unset with no realm, where nobody has a party.
        approvals_party=_approvals_party(issuer),
    )
    async with (
        connect(server, requests=InMemoryRequests(), exchange=_exchange(issuer)) as tools,
        connect(
            server, requests=InMemoryRequests(), exchange=_worker_login(issuer)
        ) as worker_tools,
    ):
        print(f"  shop           Saleor at {url}  (seed it with deploy/saleor/seed.py)")
        yield tools, worker_tools


def _exchange(issuer: ident.Issuer) -> Exchange:
    """How a session is addressed to the store. The realm's exchange when the
    agent has one, else the local issuer's — the store verifies either."""
    if issuer.url != local_issuer.URL:
        return sessions.TokenExchange.discover(
            issuer.url,
            client_id="support-agent",
            client_secret=os.environ.get(
                "SUPPORT_AGENT_CLIENT_SECRET", "local-dev-only-agent-secret"
            ),
            audience="order-system",
            scope="order-system-audience",
        )
    return local_issuer.LocalExchange()


def _notifier() -> ap.Notifier:
    """Where a waiting approval is announced. Chatwoot when the channel is
    configured — the colleague is already in it — and nobody otherwise, which
    is what a run with no channel honestly has (T-059)."""
    configured = Settings()
    if not (configured.chatwoot_base_url and configured.chatwoot_bot_token):
        return ap.Nobody()
    api = ch.ChatwootClient(configured.chatwoot_base_url, token=configured.chatwoot_bot_token)
    return ch.Inbox(api, desk_url="/ops/desk")


def _approvals_party(issuer: ident.Issuer) -> str | None:
    if issuer.url == local_issuer.URL:
        return None
    return os.environ.get("AGENT_APPROVALS_PARTY", "support-approvals")


def _worker_login(issuer: ident.Issuer) -> Exchange:
    """How the approvals worker addresses the store. Its own client at the realm
    — no customer, `orders:read`, and never `refunds:write`: the refund rests on
    the approval the store checks, not on this login. With no realm, the local
    issuer, which signs the same shape."""
    if issuer.url == local_issuer.URL:
        return local_issuer.LocalExchange()
    return sessions.ServiceLogin.discover(
        issuer.url,
        client_id=_approvals_party(issuer) or "support-approvals",
        client_secret=os.environ.get(
            "SUPPORT_APPROVALS_CLIENT_SECRET", "local-dev-only-approvals-secret"
        ),
        scope="order-system-audience",
    )


@contextlib.asynccontextmanager
async def waits_running(
    tools: ToolClient,
) -> AsyncIterator[tuple[ap.TemporalApprovals, esc.TemporalEscalations, esc.EscalationDesk]]:
    """Temporal, and the worker that holds both waits.

    One worker for approvals and escalations: the approval workflows with the
    activity that carries out a granted refund, and the escalation workflows,
    which have no activities at all. A deployment may split them; a demo should
    not pretend to.

    The address in `AGENT_TEMPORAL_ADDRESS` is the compose `durable` profile,
    whose dev server keeps its state on a volume; with none, this starts an
    ephemeral one in this process, which is why it says it is not durable —
    the same honesty the in-memory stores beside it practise.

    The worker runs here because this is a demo. A deployment runs it as its own
    process under its own login, which is the point of moving refunds into it.
    """
    address = os.environ.get("AGENT_TEMPORAL_ADDRESS")
    metrics = Settings().metrics_endpoint or None
    if address:
        client = await ap.connect_temporal(address, metrics_url=metrics)
        durable, shutdown = True, None
    else:
        from temporalio.testing import WorkflowEnvironment

        # `AGENT_TEMPORAL_UI_PORT` shows this ephemeral server's own UI, so the
        # approval and escalation workflows can be watched while the demo runs
        # all in memory — the durable server's UI sees none of them.
        ui_port = os.environ.get("AGENT_TEMPORAL_UI_PORT")
        env = await WorkflowEnvironment.start_local(
            data_converter=_CONVERTER,
            runtime=ap.metrics_runtime(metrics),
            ui=bool(ui_port),
            ui_port=int(ui_port) if ui_port else None,
        )
        if ui_port:
            print(f"  temporal ui    http://localhost:{ui_port}")
        client, durable, shutdown = env.client, False, env.shutdown
    print(f"  waits          Temporal at {client.service_client.config.target_host}", end="")
    print(" (durable)" if durable else " (in this process, lost on exit)")

    queue = ap.TASK_QUEUE
    work = ap.RefundWork(tools, acting_for=_acting_for)
    running = ap.worker(
        client,
        activities=[work.assess, work.carry_out, ap.Reminders(_notifier()).remind],
        task_queue=queue,
        workflows=[*ap.WORKFLOWS, *esc.WORKFLOWS],
    )
    try:
        async with running:
            yield (
                ap.TemporalApprovals(client, task_queue=queue, durable=durable),
                esc.TemporalEscalations(client, task_queue=queue, durable=durable),
                esc.EscalationDesk(client),
            )
    finally:
        if shutdown is not None:
            await shutdown()


async def _acting_for(customer_id: str) -> Identity:
    """The approvals worker's login. Its own in a deployment; here, the
    customer's scopes, which only a granted approval elevates."""
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


async def main(real: bool, port: int, store: bool = False, host: str = "127.0.0.1") -> None:
    settings = Settings() if real else None
    _telemetry(settings)
    issuer = _issuer()

    # The waits come up first, because the real store checks every call against
    # the approvals — so the shop needs a handle on them before it can answer.
    late = LateTools()
    async with (
        waits_running(late) as (approvals, escalations, colleagues),
        shop_running(store, issuer, approvals) as (tools, worker_tools),
    ):
        # The worker's own client, so a refund assessed an hour later is read
        # under the approvals login rather than under a session that has gone.
        late.inner = worker_tools
        # The real provider sits behind retries, a shared throttle and a breaker
        # (F-022: those existed, passed their tests, and nothing called them).
        # The scripted model cannot fail, so it has nothing to be resilient about.
        llm, run_config = await _model(settings)
        agent = ep.build(
            llm=llm,
            tools=tools,
            # One row, not three-quarters of one. This was `FileCheckpointStore`
            # beside three in-memory stores, which bought the worst of both: the
            # conversation survived a restart still saying "a colleague has your
            # refund, reference apr_3", and apr_3 did not. A folder that outlives
            # what it points at lies to the customer; a folder that dies with them
            # merely starts again. `build` now refuses the mixture outright.
            #
            # A whole durable row is now assemblable — Postgres for the
            # conversation and the ledger, Temporal for both waits and for the
            # delivery claim (T-003's last half) — and this demo does not
            # assemble it: it runs with no database on purpose. Everything here
            # is in memory together, which the check below is what enforces.
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            escalations=escalations,
            # A measured desk, so the demo shows a real wait rather than a
            # promise. Twelve an hour is invented for the demo and would be
            # measured in a deployment — the point is that the number comes from
            # somewhere rather than from the reply text.
            capacity=esc.Capacity(per_hour=12),
            deliveries=InMemoryRequests(),
            # The real model is priced, so its cost ceiling is live; the scripted
            # one is free and has nothing to meter.
            config=run_config,
            synthetic_customers=frozenset(
                c.strip() for c in Settings().synthetic_customers.split(",") if c.strip()
            ),
        )
        # The desk reads the agent's own queue and closes through its own
        # handle. Sessions come from Keycloak when AGENT_ISSUER_URL names a
        # realm, else from the process-local issuer. Nothing sweeps: an
        # escalation nobody comes to lapses on the workflow's timer (T-028).
        # Both desks on one mount: a colleague closes escalations and an
        # approver decides refunds, each under its own scope (T-059).
        app = _with_chatwoot(
            serve.build(
                agent,
                issuer=issuer,
                desk=colleagues,
                approvals=approvals,
                approver=ap.ApprovalDesk(approvals.client),
            ),
            agent,
            issuer,
        )

        _announce(port, settings, local=issuer.url == local_issuer.URL, store=store)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        await uvicorn.Server(config).serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="use a real model provider")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on. 0.0.0.0 where Chatwoot's containers must reach the "
        "agent over Docker's bridge — a Codespace or any Linux Docker; Docker Desktop "
        "routes host.docker.internal to the host's loopback, so a laptop needs neither",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="talk to the composed Saleor instead of the simulated shop (T-017)",
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.real, args.port, args.store, args.host))
    sys.exit(0)
