"""Capture one real turn end to end: calls, payloads, spans, DB rows.

Run from the reference-agent repo root:
    uv run python <this file> "show me my orders" out.json
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import uuid

REPO = pathlib.Path.cwd()
sys.path.insert(0, str(REPO))

import psycopg_pool
from evals import issuer as issuing
from starlette.requests import Request
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from order_system import server as store_server
from order_system.store import Saleor, Store
from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import serve
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.config import Settings, resolve
from support_agent.contracts import Identity
from support_agent.idempotency.postgres import PostgresLedger
from support_agent.llm import connect_model
from support_agent.resilience import ResilientLLM
from support_agent.state.postgres import PostgresCheckpointStore
from support_agent.tools import connect

TEXT = sys.argv[1] if len(sys.argv) > 1 else "show me my orders"
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "turn.json")
DSN = os.environ.get(
    "AGENT_DATABASE_URL", "postgresql://agent:local-dev-only@localhost:5433/support_agent"
)
CUSTOMER = "C-1042"
SRC = str(REPO / "src")

CAP: dict = {"calls": [], "llm": [], "mcp": [], "graphql": [], "sql": []}


# ---------------------------------------------------------------- call tree
def _short(v: object, n: int = 160) -> str:
    try:
        r = repr(v)
    except Exception:
        r = f"<{type(v).__name__}>"
    return r if len(r) <= n else r[: n - 1] + "…"


TOOL = sys.monitoring.PROFILER_ID
LIVE: dict[int, int] = {}  # frame id -> call index
SEQ = [0]


def _ours(code) -> bool:
    return code.co_filename.startswith(SRC) and not code.co_name.startswith("<")


def _depth(frame) -> int:
    d, f = 0, frame.f_back
    while f is not None:
        if id(f) in LIVE:
            d = CAP["calls"][LIVE[id(f)]]["depth"] + 1
            break
        f = f.f_back
    return d


def on_start(code, offset):
    if not _ours(code):
        return sys.monitoring.DISABLE
    frame = sys._getframe(1)
    names = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    args = {k: _short(frame.f_locals.get(k)) for k in names if k not in ("self", "cls")}
    qual = code.co_qualname
    SEQ[0] += 1
    CAP["calls"].append(
        {
            "i": SEQ[0],
            "depth": _depth(frame),
            "fn": qual,
            "file": os.path.relpath(code.co_filename, REPO),
            "line": code.co_firstlineno,
            "args": args,
            "ret": None,
            "t0": time.perf_counter(),
        }
    )
    LIVE[id(frame)] = len(CAP["calls"]) - 1


def on_return(code, offset, retval):
    if not _ours(code):
        return sys.monitoring.DISABLE
    frame = sys._getframe(1)
    idx = LIVE.pop(id(frame), None)
    if idx is not None:
        CAP["calls"][idx]["ret"] = _short(retval, 240)
        CAP["calls"][idx]["ms"] = round((time.perf_counter() - CAP["calls"][idx]["t0"]) * 1000, 2)


def on_unwind(code, offset, exc):
    if not _ours(code):
        return
    frame = sys._getframe(1)
    idx = LIVE.pop(id(frame), None)
    if idx is not None:
        CAP["calls"][idx]["ret"] = "raised " + _short(exc, 200)


def monitor(on: bool) -> None:
    E = sys.monitoring.events
    if on:
        sys.monitoring.use_tool_id(TOOL, "capture")
        sys.monitoring.register_callback(TOOL, E.PY_START, on_start)
        sys.monitoring.register_callback(TOOL, E.PY_RETURN, on_return)
        sys.monitoring.register_callback(TOOL, E.PY_UNWIND, on_unwind)
        sys.monitoring.set_events(TOOL, E.PY_START | E.PY_RETURN | E.PY_UNWIND)
    else:
        sys.monitoring.set_events(TOOL, 0)
        sys.monitoring.free_tool_id(TOOL)


# ---------------------------------------------------------------- wire taps
def tap_llm(client) -> None:
    create = client._client.chat.completions.create

    async def recording(**kwargs):
        raw = await create(**kwargs)
        CAP["llm"].append({"request": kwargs, "response": raw.model_dump(mode="json")})
        return raw

    client._client.chat.completions.create = recording


def tap_mcp(tools) -> None:
    client = tools._transport._client
    call_tool = client.call_tool

    async def recording(name, arguments, **kw):
        raw = await call_tool(name, arguments, **kw)
        CAP["mcp"].append(
            {
                "name": name,
                "arguments": arguments,
                "meta": kw.get("meta"),
                "result": raw.model_dump(mode="json") if hasattr(raw, "model_dump") else repr(raw),
            }
        )
        return raw

    client.call_tool = recording


def tap_saleor() -> None:
    post = Saleor._post

    def recording(self, query, variables):
        out = post(self, query, variables)
        CAP["graphql"].append({"query": query, "variables": variables, "response": out})
        return out

    Saleor._post = recording


async def main() -> None:
    exporter = tel.configure()
    tap_saleor()

    pool = psycopg_pool.AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await pool.open()
    issuer = issuing.issuer()

    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    queue, claims = f"waits-{uuid.uuid4().hex[:6]}", f"claims-{uuid.uuid4().hex[:6]}"
    approvals = ap.TemporalApprovals(env.client, task_queue=queue, durable=True)
    escalations = esc.TemporalEscalations(env.client, task_queue=queue, durable=True)
    deliveries = trg.TemporalDeliveries(env.client, task_queue=claims, durable=True)

    shop = Store(
        Saleor(
            os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/"),
            os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com"),
            os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only"),
        )
    )
    far = store_server.build(
        shop,
        issuer=issuing.issuer(audience="order-system"),
        clock=lambda: int(time.time()),
        approvals=approvals,
    )

    config = resolve(Settings(provider_base_url="http://localhost:4000/v1"))
    client, verified = await connect_model(
        config, api_key=os.environ.get("AGENT_GATEWAY_KEY", "sk-support-agent-local-dev-only")
    )
    tap_llm(client)

    async def acting_for(customer_id: str) -> Identity:
        return Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)

    async with connect(far, ledger=PostgresLedger(pool), exchange=issuing.LocalExchange()) as tools:
        tap_mcp(tools)
        work = ap.RefundWork(tools, acting_for=acting_for)
        async with (
            ap.worker(
                env.client,
                activities=[work.assess, work.carry_out],
                task_queue=queue,
                workflows=[*ap.WORKFLOWS, *esc.WORKFLOWS],
            ),
            trg.worker_for(env.client, task_queue=claims),
        ):
            agent = ep.build(
                llm=ResilientLLM(client),
                tools=tools,
                store=PostgresCheckpointStore(pool),
                approvals=approvals,
                escalations=escalations,
                capacity=esc.Capacity(per_hour=12),
                deliveries=deliveries,
                config=config,
            )
            app = serve.build(agent, issuer=issuer, desk=esc.EscalationDesk(env.client))

            token = issuing.mint(CUSTOMER, now=int(time.time()))
            delivery = f"dlv-{uuid.uuid4().hex[:10]}"
            body = json.dumps({"text": TEXT}).encode()
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/chat",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"authorization", f"Bearer {token}".encode()),
                    (b"idempotency-key", delivery.encode()),
                ],
                "query_string": b"",
                "app": app,
            }
            sent = [False]

            async def receive():
                if not sent[0]:
                    sent[0] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            CAP["http_request"] = {
                "method": "POST",
                "path": "/chat",
                "headers": {
                    "Authorization": "Bearer " + token[:24] + "…",
                    "Idempotency-Key": delivery,
                },
                "body": {"text": TEXT},
                "token_claims": __import__("jwt").decode(token, options={"verify_signature": False}),
            }
            monitor(True)
            try:
                response = await serve.chat(Request(scope, receive))
            finally:
                monitor(False)
            CAP["http_response"] = {
                "status": response.status_code,
                "body": json.loads(response.body),
            }

    cid = CAP["http_response"]["body"].get("conversation_id")
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT run_id, conversation_id, state, updated_at FROM agent_state.checkpoints "
                "WHERE conversation_id = %s",
                (cid,),
            )
        ).fetchall()
    CAP["checkpoints"] = [
        {
            "run_id": r[0],
            "conversation_id": r[1],
            "state": json.loads(bytes(r[2])),
            "updated_at": str(r[3]),
        }
        for r in rows
    ]
    await pool.close()
    await env.shutdown()

    CAP["spans"] = [
        {
            "name": s.name,
            "span_id": format(s.context.span_id, "016x"),
            "parent": format(s.parent.span_id, "016x") if s.parent else None,
            "trace_id": format(s.context.trace_id, "032x"),
            "ms": round((s.end_time - s.start_time) / 1e6, 2),
            "attributes": dict(s.attributes or {}),
            "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in s.events],
        }
        for s in exporter.get_finished_spans()
    ]
    CAP["config"] = {"model": config.model, "fingerprint": config.fingerprint, "verified": verified}
    for c in CAP["calls"]:
        c.pop("t0", None)
    OUT.write_text(json.dumps(CAP, indent=1, default=str))
    print("calls", len(CAP["calls"]), "llm", len(CAP["llm"]), "mcp", len(CAP["mcp"]),
          "graphql", len(CAP["graphql"]), "spans", len(CAP["spans"]),
          "checkpoints", len(CAP["checkpoints"]), "->", OUT)
    print(json.dumps(CAP["http_response"], indent=1))


asyncio.run(main())
