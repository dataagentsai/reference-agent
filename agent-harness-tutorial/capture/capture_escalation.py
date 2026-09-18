"""Capture a human escalation end to end: four steps, calls, payloads, spans, rows.

Run from the reference-agent repo root, with the default and `store` profiles up:
    uv run python agent-harness-tutorial/capture/capture_escalation.py escalation.json

The companion of `capture_turn.py`, for scenario 2. An escalation is not one turn
but a conversation and a colleague, so this drives four steps through the real
HTTP edge and records each separately:

    1. customer   "Connect me to a human"      → an escalation workflow starts
    2. customer   "hello? anyone there?"       → held, not answered by the agent
    3. colleague  GET and POST /ops/escalations → the queue, then the close
    4. customer   "where is AB-10001?"         → handed back, answered

and then reads the escalation workflow's own history from Temporal.
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

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "escalation.json")
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



STEPS = [
    ("customer", "Connect me to a human"),
    ("customer", "hello? anyone there?"),
    ("colleague", "resolve"),
    ("customer", "where is AB-10001?"),
]


def _fresh() -> dict:
    return {"calls": [], "llm": [], "mcp": [], "graphql": [], "sql": []}


async def main() -> None:
    global CAP
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

    steps: list[dict] = []
    escalation_id = conversation_id = None
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
            customer = issuing.mint(CUSTOMER, now=int(time.time()))
            desk = issuing.mint(None, scopes=ident.REVIEWER_SCOPES, subject="desk-1")
            import httpx2

            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://agent.local"
            ) as http:
                for who, text in STEPS:
                    CAP = _fresh()
                    globals()["CAP"] = CAP
                    exchanges = []
                    started = time.perf_counter()
                    monitor(True)
                    try:
                        if who == "customer":
                            delivery = f"dlv-{uuid.uuid4().hex[:10]}"
                            body = {"text": text}
                            if conversation_id:
                                body["conversation_id"] = conversation_id
                            answer = await http.post(
                                "/chat",
                                json=body,
                                headers={
                                    "authorization": f"Bearer {customer}",
                                    "idempotency-key": delivery,
                                },
                            )
                            exchanges.append(
                                {"request": {"POST": "/chat", "Idempotency-Key": delivery, "body": body},
                                 "status": answer.status_code, "response": answer.json()}
                            )
                            conversation_id = answer.json().get("conversation_id", conversation_id)
                        else:
                            listed = await http.get(
                                "/ops/escalations", headers={"authorization": f"Bearer {desk}"}
                            )
                            exchanges.append(
                                {"request": {"GET": "/ops/escalations"},
                                 "status": listed.status_code, "response": listed.json()}
                            )
                            escalation_id = listed.json()[0]["id"]
                            closing = {"outcome": "resolved", "note": "called the customer back"}
                            closed = await http.post(
                                f"/ops/escalations/{escalation_id}/resolve",
                                json=closing,
                                headers={"authorization": f"Bearer {desk}"},
                            )
                            exchanges.append(
                                {"request": {"POST": f"/ops/escalations/{escalation_id}/resolve",
                                             "body": closing},
                                 "status": closed.status_code, "response": closed.json()}
                            )
                    finally:
                        monitor(False)
                    for c in CAP["calls"]:
                        c.pop("t0", None)
                    steps.append(
                        {
                            "who": who,
                            "text": text,
                            "ms": round((time.perf_counter() - started) * 1000, 1),
                            "http": exchanges,
                            **CAP,
                        }
                    )

            history = []
            if escalation_id:
                handle = env.client.get_workflow_handle(escalation_id)
                async for event in handle.fetch_history_events():
                    history.append(
                        {
                            "id": event.event_id,
                            "type": event.WhichOneof("attributes") or "",
                            "time": event.event_time.ToDatetime().isoformat(),
                        }
                    )
                record = await escalations.get(escalation_id)

    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT run_id, state FROM agent_state.checkpoints WHERE conversation_id = %s"
                " ORDER BY updated_at",
                (conversation_id,),
            )
        ).fetchall()
    checkpoints = []
    for run_id, state in rows:
        doc = json.loads(bytes(state))
        checkpoints.append(
            {
                "run_id": run_id,
                "pending_escalation_id": doc.get("pending_escalation_id"),
                "escalations_raised": doc.get("escalations_raised"),
                "messages": len(doc.get("messages", [])),
                "facts": doc.get("facts"),
            }
        )
    await pool.close()
    await env.shutdown()

    spans = [
        {
            "name": s.name,
            "span_id": format(s.context.span_id, "016x"),
            "parent": format(s.parent.span_id, "016x") if s.parent else None,
            "trace_id": format(s.context.trace_id, "032x"),
            "ms": round((s.end_time - s.start_time) / 1e6, 2),
            "attributes": dict(s.attributes or {}),
        }
        for s in exporter.get_finished_spans()
    ]
    out = {
        "conversation_id": conversation_id,
        "escalation_id": escalation_id,
        "escalation": record.model_dump(mode="json") if escalation_id else None,
        "workflow_history": history,
        "checkpoints": checkpoints,
        "steps": steps,
        "spans": spans,
        "config": {"model": config.model, "verified": verified},
    }
    OUT.write_text(json.dumps(out, indent=1, default=str))
    for step in steps:
        print(f"{step['who']:<9} {step['text']:<26} {step['ms']:>8} ms  calls {len(step['calls']):>4}"
              f"  llm {len(step['llm'])}  mcp {len(step['mcp'])}  graphql {len(step['graphql'])}")
        for x in step["http"]:
            print("           ", x["status"], json.dumps(x["response"])[:150])
    print("history", [h["type"] for h in history])
    print("->", OUT)


asyncio.run(main())
