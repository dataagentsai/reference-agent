"""Run the agent behind HTTP, against a simulated shop.

    uv run python scripts/run_server.py           # mock model, no network, free
    uv run python scripts/run_server.py --real    # a real provider call per turn

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
from starlette.applications import Starlette
from starlette.routing import Mount
from temporalio.contrib.pydantic import pydantic_data_converter as _CONVERTER

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections.abc import AsyncIterator

from evals import issuer as local_issuer  # noqa: E402

from support_agent import approvals as ap
from support_agent import channel as ch
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import portal as ptl
from support_agent import serve
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.config import RunConfig, Settings, resolve
from support_agent.contracts import Identity, LLMClient, ModelResponse, ToolClient
from support_agent.idempotency import InMemoryLedger
from support_agent.identity import REVIEWER_SCOPES
from support_agent.identity.sessions import KeycloakLogin, KeycloakRefresh, Resume
from support_agent.llm import ScriptedClient, connect_model
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
    tel.configure()
    if settings is None or not settings.otlp_endpoint:
        return
    pairs = (p.split("=", 1) for p in settings.otlp_headers.split(",") if "=" in p)
    if tel.export_to(settings.otlp_endpoint, headers=dict(pairs)):
        print(f"  telemetry also going to {settings.otlp_endpoint}")


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


def _announce(port: int, settings: Settings | None, *, local: bool) -> None:
    """The links a person needs, with tokens from the process-local issuer."""
    if not local:
        print(f"\n  Support agent on http://127.0.0.1:{port}, sessions from the realm.")
        print("  A token: POST the realm's token endpoint as client support-chat.\n")
        return
    token = local_issuer.mint("C-1042", ttl_s=8 * 3600)
    print("\n  Support agent running against the simulated clothing shop")
    print(
        f"  model: {'REAL — ' + settings.model if settings is not None else 'scripted (free, offline)'}"
    )
    print("\n  Open this — the token is in the link:\n")
    print(f"    http://127.0.0.1:{port}/?token={token}\n")
    # The desk at /ops needs a reviewer: a login with no customer behind it,
    # signed by the same process-local issuer, so it only works against this run.
    desk = local_issuer.mint(None, scopes=REVIEWER_SCOPES, subject="desk-1", ttl_s=8 * 3600)
    print(f"  Reviewer token for /ops (GET /ops/escalations):\n\n    {desk}\n")
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


@contextlib.asynccontextmanager
async def approvals_running(tools: ToolClient) -> AsyncIterator[ap.TemporalApprovals]:
    """Temporal, and the worker that carries out what a colleague grants.

    The address in `AGENT_TEMPORAL_ADDRESS` is the compose `durable` profile,
    whose dev server keeps its state on a volume; with none, this starts an
    ephemeral one in this process, which is why it says it is not durable —
    the same honesty the in-memory stores beside it practise.

    The worker runs here because this is a demo. A deployment runs it as its own
    process under its own login, which is the point of moving refunds into it.
    """
    address = os.environ.get("AGENT_TEMPORAL_ADDRESS")
    if address:
        client, durable, shutdown = await ap.connect_temporal(address), True, None
    else:
        from temporalio.testing import WorkflowEnvironment

        env = await WorkflowEnvironment.start_local(data_converter=_CONVERTER)
        client, durable, shutdown = env.client, False, env.shutdown
    print(f"  approvals      Temporal at {client.service_client.config.target_host}", end="")
    print(" (durable)" if durable else " (in this process, lost on exit)")

    work = ap.RefundWork(tools, acting_for=_acting_for)
    try:
        async with ap.worker(client, activities=[work.assess, work.carry_out]):
            yield ap.TemporalApprovals(client, durable=durable)
    finally:
        if shutdown is not None:
            await shutdown()


async def _acting_for(customer_id: str) -> Identity:
    """The approvals worker's login. Its own in a deployment; here, the
    customer's scopes, which only a granted approval elevates."""
    return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)


async def main(real: bool, port: int) -> None:
    settings = Settings() if real else None
    _telemetry(settings)
    world = Live.start(load(WORLD))

    async with (
        connect(project(world), ledger=InMemoryLedger()) as tools,
        approvals_running(tools) as approvals,
    ):
        # The real provider sits behind retries, a shared throttle and a breaker
        # (F-022: those existed, passed their tests, and nothing called them).
        # The scripted model cannot fail, so it has nothing to be resilient about.
        llm, run_config = await _model(settings)
        escalations = esc.InMemoryEscalationStore()
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
            # The durable row is Postgres, and it is not offered here yet because
            # it is not a whole row either — the delivery log has no durable
            # implementation at all (T-003), so `--postgres` would trip the same
            # check. That is the check doing its job rather than a gap in it.
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            escalations=escalations,
            # A measured desk, so the demo shows a real wait rather than a
            # promise. Twelve an hour is invented for the demo and would be
            # measured in a deployment — the point is that the number comes from
            # somewhere rather than from the reply text.
            capacity=esc.Capacity(per_hour=12),
            deliveries=trg.InMemoryDeliveryLog(),
            # The real model is priced, so its cost ceiling is live; the scripted
            # one is free and has nothing to meter.
            config=run_config,
        )
        # The desk reads the agent's own store. Sessions come from Keycloak when
        # AGENT_ISSUER_URL names a realm, else from the process-local issuer.
        issuer = _issuer()
        app = _with_chatwoot(serve.build(agent, issuer=issuer), agent, issuer)

        _announce(port, settings, local=issuer.url == local_issuer.URL)

        # The sweeper, on a timer. `esc.sweep` owns no scheduling of its own so
        # a scenario can drive it; this is the deployment's half of that split.
        async def sweeping() -> None:
            while True:
                await asyncio.sleep(60)
                # The deployment is where the wall clock is read; everything
                # below this line takes the moment it is given.
                for lapsed in await esc.sweep(escalations, now=int(time.time())):
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
