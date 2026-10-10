"""Tier 2b — every adapter registered for a port passes that port's same rows.

Each adapter is built the way a deployment builds it: through the registry, from
an overlay's settings (`agent_harness.adapters`), never by constructing the class.
A new adapter for a port joins its table by being registered, and is held to the
rows the others pass. Rows that need an outside service skip and say which.

    model         scripted, litellm-proxy, groq-direct, apim-ai-gateway
                  — the SDK's HTTP answered in process, no network
    identity      local-dev, keycloak, entra-id — tokens signed in process, held
                  to verifying *and* to the edges (`serve`, the desk) taking
                  each adapter's verifier (claims-fnol-azure F-30)
    telemetry     console, otel-to-langfuse, azure-monitor-otel — exporters
                  and the Azure distro replaced, as `test_telemetry_azure` does
    secrets       environment-settings, key-vault — Key Vault's REST and the
                  managed identity endpoint answered in process
    approval      temporal-updates (the cached time-skipping server),
                  dbos-workflows (a throwaway PostgreSQL)
    records       in-memory, postgres — their table is `tests/test_records.py`

And one table for the composition itself: what an overlay may not say.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import jwt
import pytest
import test_dbos_waits
from temporalio import activity

import support_agent  # noqa: F401  the package root names the agent's telemetry
from agent_harness import adapters as wiring
from agent_harness import identity as ident
from agent_harness import telemetry as tel
from agent_harness.adapters import ADAPTERS, Adapter, OverlayRefused, Wiring
from agent_harness.approvals.durable import ASSESS, CARRY_OUT, FINAL, Ask, Assessment, CarriedOut
from agent_harness.approvals.workflow import Terms
from agent_harness.config.registry import UnknownName
from agent_harness.contracts import (
    Approval,
    ApprovalState,
    IdempotencyKey,
    Identity,
    Message,
    ModelRequest,
    ModelResponse,
    RunId,
    ToolCall,
    Usage,
)
from agent_harness.identity.local import KID, LocalIssuer
from support_agent.telemetry import spans

postgres = test_dbos_waits.postgres
"""A throwaway PostgreSQL for the DBOS rows, the one `test_dbos_waits` starts."""

PROFILE = Path(__file__).resolve().parents[1] / "harness-profile.yaml"


def adapter(port: str, name: str) -> Adapter:
    found: Adapter = ADAPTERS.load(port, name)
    return found


@asynccontextmanager
async def built(port: str, name: str, settings: dict[str, Any], **hooks: Any) -> AsyncIterator[Any]:
    """One adapter, built as `compose` builds it: defaults filled, hooks handed in."""
    chosen = adapter(port, name)
    filled = {k: settings.get(k, s.default) for k, s in chosen.settings.items()}
    filled |= {k: v for k, v in settings.items() if k not in filled}
    async with chosen.build(Wiring(filled, hooks.pop("built", {}), hooks, "test")) as product:
        yield product


# =========================================================================== model
class Choice:
    model, provider, temperature = "openai/gpt-oss-120b", "groq", 0.0


def completion(text: str | None, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
            }
            for c in calls
        ]
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": Choice.model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


# (row, provider's answer, the typed answer every adapter must give)
MODEL_ROWS: list[tuple[str, dict[str, Any], ModelResponse]] = [
    (
        "a text answer comes back typed, with its usage",
        completion("Your order has shipped."),
        ModelResponse(
            text="Your order has shipped.", usage=Usage(input_tokens=11, output_tokens=3)
        ),
    ),
    (
        "a tool call comes back as a ToolCall with parsed arguments",
        completion(None, [{"id": "c1", "name": "get_order", "arguments": {"id": "AB-10010"}}]),
        ModelResponse(
            tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10010"}),),
            usage=Usage(input_tokens=11, output_tokens=3),
        ),
    ),
]
MODEL_ADAPTERS: list[tuple[str, dict[str, Any], str]] = [
    # (adapter, overlay settings, where the key must travel)
    ("scripted", {}, ""),
    ("litellm-proxy", {"api_key": "k-1", "base_url": "http://proxy.test/v1"}, "authorization"),
    ("groq-direct", {"api_key": "k-1", "base_url": "http://groq.test/v1"}, "authorization"),
    (
        "apim-ai-gateway",
        {"api_key": "k-1", "base_url": "http://apim.test/v1"},
        "ocp-apim-subscription-key",
    ),
]
ASK = ModelRequest(messages=(Message(role="user", content="Where is AB-10010?"),))


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(
    ("name", "settings", "key_at"), MODEL_ADAPTERS, ids=[a[0] for a in MODEL_ADAPTERS]
)
@pytest.mark.parametrize(("row", "answer", "typed"), MODEL_ROWS, ids=[r[0] for r in MODEL_ROWS])
async def test_the_model_port(
    name: str,
    settings: dict[str, Any],
    key_at: str,
    row: str,
    answer: dict[str, Any],
    typed: ModelResponse,
) -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    http = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    hooks = {"choice": Choice(), "http_client": http, "script": [typed]}
    async with built("model", name, settings, **hooks) as client:
        got = await client.complete(ASK)
    assert (got.text, got.tool_calls, got.usage.input_tokens, got.usage.output_tokens) == (
        typed.text,
        typed.tool_calls,
        typed.usage.input_tokens,
        typed.usage.output_tokens,
    )
    if key_at:
        (sent,) = seen
        assert "k-1" in sent.headers.get(key_at, "")
        assert json.loads(sent.content)["messages"][-1]["content"] == "Where is AB-10010?"


# ======================================================================== identity
TENANT = "00000000-0000-0000-0000-0000000000aa"
SIGNER = LocalIssuer(url="http://keycloak.test/realms/shop", audience="support-agent")
STRANGER = LocalIssuer(url=SIGNER.url, audience=SIGNER.audience)
"""Another key under the same names: what a forged session is signed with."""


DESK_ROLE = "desk.handler"
DESK_SCOPES = ident.REVIEWER_SCOPES | ident.APPROVER_SCOPES
"""What the desk role grants. Keycloak's realm (and the local issuer) write the
scopes into `scp`; Entra writes the app role's value into `roles`, and the
overlay's `role_scopes` says what it grants."""


def mint(
    name: str,
    *,
    audience: str = "support-agent",
    ttl_s: int = 600,
    key: LocalIssuer = SIGNER,
    handler: bool = False,
) -> str:
    """A session in the shape this adapter's issuer writes it: a customer's, or
    with `handler`, a desk handler's (no customer behind it)."""
    now = int(time.time())
    if name == "entra-id":
        claims: dict[str, Any] = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "aud": audience,
            "sub": "login-1",
            "uti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + ttl_s,
        }
        if handler:
            claims["roles"] = [DESK_ROLE]
        else:
            claims |= {"scp": "orders:read orders:write", ident.CLAIM_CUSTOMER: "C-1042"}
        return jwt.encode(claims, key._key, algorithm="RS256", headers={"kid": KID})
    signer = LocalIssuer(url=SIGNER.url, audience=audience, _key=key._key)
    if handler:
        return signer.mint(subject="login-9", scopes=DESK_SCOPES, ttl_s=ttl_s)
    return signer.mint(
        subject="login-1", scopes={"orders:read", "orders:write"}, customer_id="C-1042", ttl_s=ttl_s
    )


IDENTITY_ADAPTERS: list[tuple[str, dict[str, Any]]] = [
    ("local-dev", {"url": SIGNER.url, "audience": "support-agent"}),
    ("keycloak", {"issuer_url": SIGNER.url, "audience": "support-agent"}),
    (
        "entra-id",
        {
            "tenant": TENANT,
            "audience": "support-agent",
            "role_scopes": {DESK_ROLE: sorted(DESK_SCOPES)},
        },
    ),
]
# (row, how the session is made, whom it verifies as or None for refused)
IDENTITY_ROWS: list[tuple[str, dict[str, Any], str | None]] = [
    ("a session for this agent verifies as its customer", {}, "C-1042"),
    ("one for another audience is refused", {"audience": "order-system"}, None),
    ("an expired one is refused", {"ttl_s": -120}, None),
    ("one signed by another key is refused", {"key": STRANGER}, None),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(
    ("name", "settings"), IDENTITY_ADAPTERS, ids=[a[0] for a in IDENTITY_ADAPTERS]
)
@pytest.mark.parametrize(
    ("row", "made", "customer"), IDENTITY_ROWS, ids=[r[0] for r in IDENTITY_ROWS]
)
async def test_the_identity_port(
    name: str, settings: dict[str, Any], row: str, made: dict[str, Any], customer: str | None
) -> None:
    async with built("identity", name, settings, keys=ident.JWKS(SIGNER.jwks)) as sessions:
        if name == "local-dev":  # its own key: the signer is part of the product
            token = mint(name, **{**made, "key": made.get("key", sessions.signer)})
        else:
            token = mint(name, **made)
        if customer is None:
            with pytest.raises(ident.InvalidSession):
                sessions.verify(token)
            return
        principal = sessions.verify(token)
    assert principal.customer_id == customer
    assert {"orders:read", "orders:write"} <= principal.scopes


class _EmptyQueue:
    """Escalations and approvals with nothing waiting: the edge rows are about who
    gets in, not about what a queue holds."""

    async def pending(self) -> tuple[()]:
        return ()

    async def get(self, _: str) -> None:
        return None

    async def open_for(self, _: str) -> None:
        return None


class _NoTurns:
    """A ready agent (`TurnAgent`) that is never asked for a turn: `/feedback`
    reads its store, and nothing here posts to `/chat`."""

    def __init__(self) -> None:
        from agent_harness.state import InMemoryCheckpointStore

        self.store = InMemoryCheckpointStore()
        self.escalations = _EmptyQueue()

    async def opening(self, identity: Identity) -> str:
        raise AssertionError("not under test")

    async def handle(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("not under test")


# (row, the session, method, path, the status the edge must answer)
EDGE_ROWS: list[tuple[str, dict[str, Any], str, str, int]] = [
    (
        "a handler with the desk role reads the escalation queue",
        {"handler": True},
        "GET",
        "/ops/escalations",
        200,
    ),
    (
        "a handler with the desk role reads what waits for approval",
        {"handler": True},
        "GET",
        "/ops/approvals",
        200,
    ),
    (
        "a policyholder without the desk role is refused at the desk",
        {},
        "GET",
        "/ops/approvals",
        403,
    ),
    (
        "a policyholder's session is accepted at the customer edge",
        {},
        "POST",
        "/feedback",
        404,
    ),  # past identity: the conversation is not theirs
    (
        "a handler's session is not a customer's at the customer edge",
        {"handler": True},
        "POST",
        "/feedback",
        403,
    ),
    (
        "a forged desk session is refused at the desk",
        {"handler": True, "key": STRANGER},
        "GET",
        "/ops/approvals",
        401,
    ),
]


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize(
    ("name", "settings"), IDENTITY_ADAPTERS, ids=[a[0] for a in IDENTITY_ADAPTERS]
)
@pytest.mark.parametrize(
    ("row", "made", "method", "path", "status"), EDGE_ROWS, ids=[r[0] for r in EDGE_ROWS]
)
async def test_the_edges_verify_through_the_identity_port(
    name: str,
    settings: dict[str, Any],
    row: str,
    made: dict[str, Any],
    method: str,
    path: str,
    status: int,
) -> None:
    """F-30: `serve` and the desk take the verifier of the adapter the
    composition chose, so each issuer's sessions are read in its own shape at the
    edge, not only by `Sessions.verify`."""
    from agent_harness import serve

    async with built("identity", name, settings, keys=ident.JWKS(SIGNER.jwks)) as sessions:
        if name == "local-dev":
            made = {**made, "key": made.get("key", sessions.signer)}
        token = mint(name, **made)
        queue = _EmptyQueue()
        app = serve.build(
            _NoTurns(),
            verify=sessions.verify,
            chat_page="",
            approvals=queue,  # type: ignore[arg-type]
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://edge.test"
        ) as client:
            answer = await client.request(
                method,
                path,
                headers={"authorization": f"Bearer {token}"},
                json={"conversation_id": "cnv_none", "value": "up"} if method == "POST" else None,
            )
    assert answer.status_code == status, (row, answer.text)


def test_a_misshapen_role_mapping_is_refused_at_startup() -> None:
    async def build() -> None:
        settings = {"tenant": TENANT, "audience": "a", "role_scopes": {DESK_ROLE: "approvals:read"}}
        async with built("identity", "entra-id", settings, keys=ident.JWKS(SIGNER.jwks)):
            pass

    with pytest.raises(OverlayRefused, match="role_scopes"):
        asyncio.run(build())


# ========================================================================= secrets
class _Answer:
    """What `urllib.request.urlopen` gives back: a JSON body, as a context manager."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, *_: Any) -> bytes:
        return self._body


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Key Vault's REST answered in process: every secret reads "from-the-vault"."""
    from agent_harness.adapters import secrets

    asked: list[str] = []

    def urlopen(request: Any, timeout: float = 0) -> _Answer:
        asked.append(request.full_url)
        return _Answer({"value": "from-the-vault", "access_token": "mi-token"})

    monkeypatch.setattr(secrets.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("TEST_GREETING", "hello")
    monkeypatch.delenv("TEST_MISSING", raising=False)
    monkeypatch.setenv("TEST_VAULT_URL", "https://kv-test.vault.azure.net/")
    return asked


SECRETS_ADAPTERS: list[tuple[str, str]] = [
    ("environment-settings", "{adapter: environment-settings, why: tests}"),
    # The vault's address from the environment: the deployment's output, never
    # a name written into the overlay (claims-fnol-azure, Tier 3).
    ("key-vault", "{adapter: key-vault, vault_url: {env: TEST_VAULT_URL}, why: tests}"),
]
# (row, the reference, what each adapter reads it as; None is a refusal)
SECRETS_ROWS: list[tuple[str, dict[str, Any], dict[str, str | None]]] = [
    (
        "an environment reference reads the environment",
        {"env": "TEST_GREETING"},
        {"environment-settings": "hello", "key-vault": "hello"},
    ),
    (
        "a missing variable takes the reference's default",
        {"env": "TEST_MISSING", "default": "d"},
        {"environment-settings": "d", "key-vault": "d"},
    ),
    (
        "a missing variable with no default is refused",
        {"env": "TEST_MISSING"},
        {"environment-settings": None, "key-vault": None},
    ),
    (
        "a vault reference is read from the vault, or refused where there is none",
        {"key_vault": "groq-api-key"},
        {"environment-settings": None, "key-vault": "from-the-vault"},
    ),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(
    ("name", "binding"), SECRETS_ADAPTERS, ids=[a[0] for a in SECRETS_ADAPTERS]
)
@pytest.mark.parametrize(
    ("row", "reference", "reads"), SECRETS_ROWS, ids=[r[0] for r in SECRETS_ROWS]
)
async def test_the_secrets_port(
    tmp_path: Path,
    vault: list[str],
    name: str,
    binding: str,
    row: str,
    reference: dict[str, Any],
    reads: dict[str, str | None],
) -> None:
    planned = wiring.plan(overlay(tmp_path, f"  secrets: {binding}\n"))
    hooks = {"secrets": {"token": lambda: "mi-token"}}
    async with wiring.compose(planned, hooks=hooks, ports=("secrets",)) as built:
        reader = built["secrets"]
        if reads[name] is None:
            with pytest.raises(OverlayRefused):
                reader.read(reference)
            return
        assert reader.read(reference) == reads[name], row
    if "key_vault" in reference:
        assert vault == ["https://kv-test.vault.azure.net/secrets/groq-api-key?api-version=7.4"]


def test_the_secrets_ports_own_settings_name_only_the_environment(tmp_path: Path) -> None:
    binding = "  secrets: {adapter: key-vault, vault_url: {key_vault: where}, why: tests}\n"
    planned = wiring.plan(overlay(tmp_path, binding))

    async def build() -> None:
        async with wiring.compose(planned, ports=("secrets",)):
            pass

    with pytest.raises(OverlayRefused, match="reads only"):
        asyncio.run(build())


# (row, the app's environment, the client_id the token request must carry)
IDENTITY_TOKEN_ROWS: list[tuple[str, dict[str, str], str | None]] = [
    (
        "a user-assigned identity is named by AZURE_CLIENT_ID",
        {
            "IDENTITY_ENDPOINT": "http://mi.test/token",
            "IDENTITY_HEADER": "h",
            "AZURE_CLIENT_ID": "c-1",
        },
        "c-1",
    ),
    (
        "without it the system-assigned identity is asked for",
        {"IDENTITY_ENDPOINT": "http://mi.test/token", "IDENTITY_HEADER": "h"},
        None,
    ),
]


@pytest.mark.parametrize(
    ("row", "environ", "client_id"), IDENTITY_TOKEN_ROWS, ids=[r[0] for r in IDENTITY_TOKEN_ROWS]
)
def test_the_key_vault_token_asks_for_the_apps_own_identity(
    vault: list[str], row: str, environ: dict[str, str], client_id: str | None
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from agent_harness.adapters.secrets import managed_identity_token

    assert managed_identity_token(environ) == "mi-token"
    (url,) = vault
    asked = parse_qs(urlsplit(url).query)
    assert asked.get("client_id") == ([client_id] if client_id else None), row
    assert asked["resource"] == ["https://vault.azure.net"]


# ======================================================================= telemetry
class FakeDistro:
    """`configure_azure_monitor`, building providers as the distro does; no network."""

    def __init__(self) -> None:
        from opentelemetry import metrics, trace

        self.calls: list[dict[str, Any]] = []
        self.tracer: Any = trace.ProxyTracerProvider()
        self.meter: Any = metrics.NoOpMeterProvider()

    def __call__(self, **kwargs: Any) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.trace import TracerProvider

        self.calls.append(kwargs)
        self.tracer = TracerProvider(resource=kwargs["resource"])
        for processor in kwargs["span_processors"]:
            self.tracer.add_span_processor(processor)
        self.meter = MeterProvider(
            resource=kwargs["resource"], metric_readers=kwargs["metric_readers"]
        )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The two places a telemetry adapter would leave the process, replaced."""
    import azure.monitor.opentelemetry as distro
    from opentelemetry import metrics, trace

    fake, exported = FakeDistro(), {}
    monkeypatch.setattr(distro, "configure_azure_monitor", fake)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: fake.tracer)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: fake.meter)
    monkeypatch.setattr(
        tel,
        "export_to",
        lambda endpoint, headers=None: exported.update(endpoint=endpoint, headers=headers) or True,
    )
    yield {"distro": fake, "exported": exported}
    tel.identify(scope=spans.SCOPE, service=spans.SERVICE)
    tel.configure()


TELEMETRY_ADAPTERS: list[tuple[str, dict[str, Any], str]] = [
    ("console", {}, ""),
    (
        "otel-to-langfuse",
        {"endpoint": "http://collector.test/v1/traces", "headers": "x-key=k-1"},
        "exported",
    ),
    (
        "azure-monitor-otel",
        {"connection_string": "InstrumentationKey=00000000-0000-0000-0000-000000000000"},
        "distro",
    ),
]
TELEMETRY_ROWS: list[tuple[str, str | None, str]] = [
    ("the agent's own service name", None, "support-agent"),
    ("a service name the overlay sets", "support-agent-canary", "support-agent-canary"),
]


@pytest.mark.discharges("AHC-0111")
@pytest.mark.parametrize(
    ("name", "settings", "leaves"), TELEMETRY_ADAPTERS, ids=[a[0] for a in TELEMETRY_ADAPTERS]
)
@pytest.mark.parametrize(
    ("row", "service", "expected"), TELEMETRY_ROWS, ids=[r[0] for r in TELEMETRY_ROWS]
)
async def test_the_telemetry_port(
    no_network: dict[str, Any],
    name: str,
    settings: dict[str, Any],
    leaves: str,
    row: str,
    service: str | None,
    expected: str,
) -> None:
    async with built("telemetry", name, {**settings, "service": service}) as exporter:
        with tel.span("agent.direct", **{"agent.handler": "order_status"}):
            pass
    (finished,) = exporter.get_finished_spans()
    assert finished.resource.attributes["service.name"] == expected
    assert finished.resource.attributes["deployment.environment.name"] == "test"
    assert tel.validate(exporter.get_finished_spans()) == []
    if leaves == "exported":
        assert no_network["exported"] == {
            "endpoint": settings["endpoint"],
            "headers": {"x-key": "k-1"},
        }
    if leaves == "distro":
        (call,) = no_network["distro"].calls
        assert call["connection_string"] == settings["connection_string"]


# ======================================================================== approval
REMIND_BEFORE_S = 0
"""No reminder: the whole day is one wait. Temporal expired these at once until
this table ran the row against it (approvals.durable, Tier 2b)."""


class Steps:
    """The agent's side of a wait, as both engines run it."""

    def __init__(self, person: bool) -> None:
        self.person, self.carried = person, 0

    @activity.defn(name=ASSESS)
    async def assess(self, ask: Ask) -> Assessment:
        reason = "above the automatic limit" if self.person else None
        return Assessment(args=dict(ask.args), reason=reason, approver="policy")

    @activity.defn(name=CARRY_OUT)
    async def carry_out(self, approval: Approval) -> CarriedOut:
        self.carried += 1
        return CarriedOut(ok=True, text="carried out")


class NoTools:
    """The tool port's second connection, which these steps never use."""

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        yield None


@asynccontextmanager
async def waits(name: str, server: str, steps: Steps) -> AsyncIterator[Any]:
    hooks: dict[str, Any] = {
        "work": lambda _tools: steps,
        "terms": Terms(ttl_s=86_400, remind_before_s=REMIND_BEFORE_S),
    }
    if name == "temporal-updates":
        from temporalio.contrib.pydantic import pydantic_data_converter
        from temporalio.testing import WorkflowEnvironment

        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
        async with env:
            settings = {"task_queue": f"contract-{uuid.uuid4().hex[:8]}", "durable": False}
            async with built(
                "approval",
                name,
                settings,
                client=env.client,
                built={"tool_runtime": NoTools()},
                **hooks,
            ) as made:
                yield made
        return
    settings = {"url": f"{server}/contract_{uuid.uuid4().hex[:10]}", "name": "contract-test"}
    async with built(
        "approval", name, settings, built={"tool_runtime": NoTools()}, **hooks
    ) as made:
        yield made


async def settled(read: Any, approval_id: str, final: str) -> Approval:
    for _ in range(200):
        found = await read.get(approval_id)
        if found is not None and found.state in FINAL and found.state.value == final:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"{approval_id} never reached {final}: {found}")


WAIT_ADAPTERS = ["temporal-updates", "dbos-workflows"]
# (row, a person must decide, their decision or None, final state, times carried out)
APPROVAL_ROWS: list[tuple[str, bool, bool | None, str, int]] = [
    ("within the automatic limit: carried out at once", False, None, "done", 1),
    ("above it: waits, and a person grants it", True, True, "done", 1),
    ("above it: a person declines it", True, False, "refused", 0),
]


@pytest.mark.discharges("AHC-0057", "P-APPROVAL-QUEUE")
@pytest.mark.parametrize("name", WAIT_ADAPTERS)
@pytest.mark.parametrize(
    ("row", "person", "granted", "final", "carried"),
    APPROVAL_ROWS,
    ids=[r[0] for r in APPROVAL_ROWS],
)
def test_the_approval_port(
    postgres: str, name: str, row: str, person: bool, granted: bool | None, final: str, carried: int
) -> None:
    steps = Steps(person)

    async def scenario() -> tuple[Approval, tuple[Approval, ...]]:
        async with waits(name, postgres, steps) as made:
            asked = await made.approvals.request(
                action="issue_refund",
                args={"order_id": "AB-201"},
                identity=Identity(customer_id="C-1042", scopes=frozenset({"orders:read"})),
                idempotency_key=IdempotencyKey(
                    run_id=RunId(f"run_{uuid.uuid4().hex[:6]}"), step=1, iteration=1
                ),
            )
            waiting = await made.approvals.pending()
            if granted is not None:
                assert asked.state is ApprovalState.WAITING
                await made.approver.decide(asked.id, granted=granted, by="ops-7")
            return await settled(made.approvals, asked.id, final), waiting

    found, waiting = asyncio.run(scenario())
    assert (found.state.value, steps.carried) == (final, carried)
    assert (found.id in {a.id for a in waiting}) is person


@pytest.mark.discharges("P-ESC-QUEUE")
@pytest.mark.parametrize("name", WAIT_ADAPTERS)
def test_the_escalation_side_of_the_port(postgres: str, name: str) -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        async with waits(name, postgres, Steps(False)) as made:
            raised = await made.escalations.raise_for(
                conversation_id="conv-1",
                run_id="run-1",
                customer_id="C-1042",
                reason="asked for a person",
                rule_id="esc:asked",
                rules_version="1",
            )
            held = await made.escalations.open_for("conv-1")
            queued = raised.id in {e.id for e in await made.escalations.pending()}
            await made.desk.resolve(raised.id, outcome="resolved", by="ops-7")
            for _ in range(200):
                if await made.escalations.open_for("conv-1") is None:
                    break
                await asyncio.sleep(0.05)
            return (
                held is not None and held.id == raised.id,
                queued,
                await made.escalations.open_for("conv-1") is None,
            )

    assert asyncio.run(scenario()) == (True, True, True)


# ===================================================================== composition
def overlay(tmp_path: Path, bindings: str, top: str = "") -> Path:
    path = tmp_path / "local.yaml"
    path.write_text(
        "apiVersion: harness-overlay/v1\nenvironment: local\n"
        f"profile: {PROFILE}\n{top}bindings:\n{bindings}"
    )
    return path


REFUSED: list[tuple[str, str, str, str]] = [
    (
        "an adapter nobody registered",
        "  model: {adapter: crystal-ball}\n",
        "",
        "has no 'crystal-ball'; known: apim-ai-gateway",
    ),
    ("a port nobody composes", "  oracle: {adapter: x}\n", "", "unknown field 'oracle'"),
    (
        "a setting the adapter does not read",
        "  telemetry: {adapter: console, colour: red, why: dev}\n",
        "",
        "unknown field 'colour'",
    ),
    (
        "a secret given by value",
        "  model: {adapter: groq-direct, api_key: gsk_live, why: dev}\n",
        "",
        "is a secret: give a reference",
    ),
    (
        "a required setting left out",
        "  model: {adapter: apim-ai-gateway, api_key: {env: K}, why: x}\n",
        "",
        "base_url is required",
    ),
    (
        "leaving the profile without saying why",
        "  model: {adapter: scripted}\n",
        "",
        "say `why` it differs here",
    ),
    (
        "an unknown top-level field",
        "  model: {adapter: scripted, why: tests}\n",
        "colour: red\n",
        "unknown field 'colour'",
    ),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(("row", "bindings", "top", "says"), REFUSED, ids=[r[0] for r in REFUSED])
def test_an_overlay_that_cannot_compose_is_refused_at_startup(
    tmp_path: Path, row: str, bindings: str, top: str, says: str
) -> None:
    with pytest.raises((OverlayRefused, UnknownName)) as refused:
        wiring.plan(overlay(tmp_path, bindings, top))
    assert says in str(refused.value)


@pytest.mark.discharges("AHC-0022")
def test_every_registered_adapter_loads_and_names_its_port() -> None:
    for port in ADAPTERS.slots():
        for name in ADAPTERS.names(port):
            found = adapter(port, name)
            assert (found.port, found.name) == (port, name)
