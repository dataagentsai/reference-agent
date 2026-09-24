"""A chat turn arrives in Langfuse, findable and priced (T-052).

    docker compose --profile obs up -d
    uv run python deploy/langfuse/models.py

The stack file said telemetry went to Langfuse from 16 September, and nothing
had ever looked for a trace there. This does: one turn over the real HTTP edge,
exported the way a deployment exports it, then found in Langfuse by the
conversation it belongs to. Skipped when no Langfuse answers.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx2 as httpx
import pytest
from agenttwin import Live, load, project
from evals import issuer as issuing
from opentelemetry import trace

from support_agent import entrypoint as ep
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.contracts import ModelResponse, ToolCall
from support_agent.cost import PRICES
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

URL = os.environ.get("LANGFUSE_URL", "http://localhost:3000")
KEYS = (
    os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-dev-only"),
    os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-dev-only"),
)
WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"


def langfuse(path: str, *, timeout: int = 20) -> dict:
    auth = base64.b64encode(":".join(KEYS).encode()).decode()
    request = urllib.request.Request(f"{URL}{path}", headers={"authorization": f"Basic {auth}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


@pytest.fixture(scope="module")
def reachable() -> None:
    try:
        langfuse("/api/public/health")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"no Langfuse at {URL}: {exc}")


def observations_of(session: str, *, wait_s: int = 60) -> list[dict]:
    """The trace for this conversation, once Langfuse has ingested it. Ingestion
    is asynchronous — a queue, then ClickHouse — so this polls."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            found = langfuse("/api/public/v2/observations?limit=200&fields=core,basic,usage,model")
        except (urllib.error.URLError, TimeoutError, OSError):
            # Langfuse under load answers slowly, and a poll that timed out is
            # not an absence of the trace — it is one poll. Keep polling until
            # the deadline (it failed this way once, in a 48-minute suite).
            time.sleep(2)
            continue
        roots = [o for o in found["data"] if o.get("sessionId") == session]
        if roots:
            trace_id = roots[0]["traceId"]
            return [o for o in found["data"] if o["traceId"] == trace_id]
        time.sleep(2)
    return []


@pytest.mark.discharges("AAC-0011", "AAC-0060")
async def test_a_turn_is_a_findable_priced_trace_in_langfuse(reachable: None) -> None:
    """The customer and the conversation on the root, so the trace can be found
    by either; the model call as a generation with its tokens."""
    tel.configure()
    auth = base64.b64encode(":".join(KEYS).encode()).decode()
    assert tel.export_to(
        f"{URL}/api/public/otel/v1/traces", headers={"Authorization": f"Basic {auth}"}
    )

    script = ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10003"}),)
            ),
            ModelResponse(text="AB-10003 was delivered five days ago."),
        ]
    )
    live = Live.start(load(WORLD))
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=script, tools=tools, store=InMemoryCheckpointStore())
        app = serve.build(agent, issuer=issuing.issuer())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            answer = await client.post(
                "/chat",
                headers={"authorization": f"Bearer {issuing.mint('C-1042')}"},
                json={"text": "could you tell me about my order AB-10003, please explain"},
            )
    session = answer.json()["conversation_id"]
    trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]

    found = observations_of(session)
    assert found, f"no trace for {session} reached Langfuse"
    root = next(o for o in found if not o.get("parentObservationId"))
    assert (root["name"], root["userId"], root["sessionId"]) == ("http.chat", "C-1042", session)
    assert any(o["type"] == "GENERATION" for o in found), [o["name"] for o in found]


@pytest.mark.tooling
def test_langfuse_prices_every_model_the_agent_can_use(reachable: None) -> None:
    """From the agent's own price table (`deploy/langfuse/models.py`), so a
    model added to the table and not to Langfuse shows up here, unpriced."""
    known: set[str] = set()
    page, pages = 1, 1
    while page <= pages:
        found = langfuse(f"/api/public/models?limit=100&page={page}")
        known |= {m["modelName"] for m in found["data"]}
        pages, page = found["meta"]["totalPages"], page + 1
    assert set(PRICES) <= known, sorted(set(PRICES) - known)
