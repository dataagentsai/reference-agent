"""The agent watched from outside: metrics, the evaluation record, the watch,
feedback and the canary, end to end through the real HTTP edge (T-055–T-057).

One app over the projected shop, a scripted model, and the in-memory span
exporter standing in for Langfuse: the watch reads the spans in exactly the
shape Langfuse returns them, so what passes here is what runs against it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agenttwin import Live, load, project
from evals import issuer as issuing
from opentelemetry.sdk.trace import ReadableSpan
from starlette.testclient import TestClient

from support_agent import entrypoint as ep
from support_agent import serve
from support_agent import telemetry as tel
from support_agent.contracts import ModelResponse, ToolCall
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect
from support_agent.watch import Watch, canary, record
from support_agent.watch.outcomes import Outcome
from support_agent.watch.rules import Finding, Verdict

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
ISSUER = issuing.issuer()


def token(customer: str = "C-1042") -> str:
    return issuing.mint(customer, now=int(time.time()))


def app_with(replies: list[ModelResponse], *, synthetic: frozenset[str] = frozenset()):
    world = Live.start(load(WORLD))
    store = InMemoryCheckpointStore()

    @asynccontextmanager
    async def make_agent():
        async with connect(project(world), requests=InMemoryRequests()) as tools:
            yield ep.build(
                llm=ScriptedClient(replies),
                tools=tools,
                store=store,
                synthetic_customers=synthetic,
            )

    return TestClient(serve.build(make_agent, issuer=ISSUER))


def chat(client: TestClient, text: str, customer: str = "C-1042", cid: str | None = None):
    body: dict[str, Any] = {"text": text}
    if cid:
        body["conversation_id"] = cid
    return client.post("/chat", json=body, headers={"authorization": f"Bearer {token(customer)}"})


def observations(spans: Iterable[ReadableSpan]) -> list[dict[str, Any]]:
    """Finished spans as Langfuse's v2 observations API returns them."""

    def iso(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1e9, UTC).isoformat().replace("+00:00", "Z")

    out = []
    for span in spans:
        scope = span.instrumentation_scope
        metadata: dict[str, Any] = {"scope.name": scope.name if scope else None}
        metadata |= {f"attributes.{k}": v for k, v in (span.attributes or {}).items()}
        out.append(
            {
                "traceId": format(span.context.trace_id, "032x"),
                "name": span.name,
                "startTime": iso(span.start_time or 0),
                "endTime": iso(span.end_time or 0),
                "metadata": metadata,
            }
        )
    return out


class Spans:
    """A Source over the in-memory exporter."""

    def __init__(self, exporter) -> None:
        self.exporter = exporter

    def _all(self) -> list[dict[str, Any]]:
        return observations(self.exporter.get_finished_spans())

    def traces_between(self, start: float, end: float) -> list[Mapping[str, Any]]:
        return self._all()

    def turns_of(self, user: str, start: float, end: float) -> list[Mapping[str, Any]]:
        return [
            o
            for o in self._all()
            if o["name"] == "agent.turn" and o["metadata"].get("attributes.user.id") == user
        ]

    def feedback_between(self, start: float, end: float) -> list[Mapping[str, Any]]:
        return [o for o in self._all() if o["name"] == "agent.feedback"]


class Kept:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.passes: list[Verdict] = []
        self.scored: list[tuple[str, int]] = []
        self.versions: set[str] = set()
        self.outcomes: list[Outcome] = []

    def finding(self, found: Finding) -> None:
        self.findings.append(found)

    def passed(self, verdict: Verdict) -> None:
        self.passes.append(verdict)

    def evaluated(self, trace_id: str, findings: int, version: str) -> None:
        self.scored.append((trace_id, findings))
        self.versions.add(version)

    def outcome(self, found: Outcome) -> None:
        self.outcomes.append(found)


def total(name: str, **labels: str) -> float:
    return sum(
        getattr(point, "value", getattr(point, "count", 0))
        for attributes, point in tel.metric_points(name)
        if all(attributes.get(k) == v for k, v in labels.items())
    )


# --------------------------------------------------------------------------- #
# Metrics (T-055, AHC-0111).
# --------------------------------------------------------------------------- #

LOOKUP = [
    ModelResponse(tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10002"}),)),
    ModelResponse(text="AB-10002 is pending and has not shipped yet."),
]
CANCEL_SHIPPED = [
    ModelResponse(
        tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": "AB-10001"}),)
    ),
    ModelResponse(text="AB-10001 has shipped, so it can no longer be cancelled."),
]

# (why, the model's script, the message, the tool outcome counted)
TOOLS = [
    ("a read that answers", LOOKUP, "I have a question about AB-10002", ("get_order", "ok")),
    (
        "a write the store refuses",
        CANCEL_SHIPPED,
        "Please cancel AB-10001",
        ("cancel_order", "refused"),
    ),
]


@pytest.mark.parametrize(("why", "script", "text", "counted"), TOOLS, ids=[r[0] for r in TOOLS])
@pytest.mark.discharges("AHC-0111", "AAC-0114")
def test_a_turn_counts_itself_its_duration_and_its_tools(
    why: str, script: list[ModelResponse], text: str, counted: tuple[str, str]
) -> None:
    tel.configure()
    with app_with(script) as client:
        assert chat(client, text).status_code == 200

    assert total("agent.turns", result="completed", synthetic="false") == 1
    assert total("agent.turn.duration", result="completed") == 1  # one observation
    assert total("agent.tool.calls", tool=counted[0], outcome=counted[1]) == 1
    assert total("agent.tool.duration", tool=counted[0]) == 1
    assert total("agent.run.terminations", reason="goal_reached") == 1


@pytest.mark.discharges("AHC-0113")
def test_the_canarys_turns_are_counted_apart() -> None:
    tel.configure()
    with app_with(LOOKUP, synthetic=frozenset({"C-7001"})) as client:
        chat(client, "Where is my order CN-70002?", customer="C-7001")
    assert total("agent.turns", synthetic="true") == 1
    assert total("agent.turns", synthetic="false") == 0


# (why, the attributes on a finished run span, termination counted, spend observations)
RUNS = [
    ("a priced run", {tel.TERMINATION: "goal_reached", tel.COST_USD: 0.003}, "goal_reached", 1),
    ("an unpriced run", {tel.TERMINATION: "step_budget_exhausted"}, "step_budget_exhausted", 0),
]


@pytest.mark.parametrize(("why", "attributes", "reason", "spent"), RUNS, ids=[r[0] for r in RUNS])
@pytest.mark.discharges("AAC-0008", "AHC-0111")
def test_a_run_is_counted_once_where_it_ends(
    why: str, attributes: dict[str, Any], reason: str, spent: int
) -> None:
    tel.configure()
    with tel.span("agent.run", **{tel.RUN_ID: "run_x", tel.TENANT: "C-1042"}) as span:
        for k, v in attributes.items():
            span.set_attribute(k, v)
    assert total("agent.run.terminations", reason=reason) == 1
    assert total("agent.spend") == spent


# (why, capture on, sample rate, whether a turn is captured)
CAPTURE = [
    ("off by default", False, 1.0, False),
    ("on, every turn", True, 1.0, True),
    ("on, no turns", True, 0.0, False),
]


@pytest.mark.parametrize(("why", "on", "rate", "kept"), CAPTURE, ids=[r[0] for r in CAPTURE])
@pytest.mark.discharges("AHC-0114", "AAC-0095")
def test_a_turns_words_are_kept_only_when_it_is_sampled(
    why: str, on: bool, rate: float, kept: bool
) -> None:
    exporter = tel.configure(capture_payloads=on, capture_sample=rate)
    with app_with(LOOKUP) as client:
        chat(client, "I have a question about AB-10002")
    turn = next(s for s in exporter.get_finished_spans() if s.name == "agent.turn")
    attributes = tel.attributes_of(turn)
    assert attributes[tel.CAPTURED] is kept
    assert (tel.REPLY in attributes) is kept
    assert (tel.INPUT in attributes) is kept
    tel.configure()


def test_sampling_is_decided_by_the_run_id_so_a_replay_decides_the_same() -> None:
    tel.configure(capture_payloads=True, capture_sample=0.5)
    decided = [tel.capture_decision(f"run_{i}") for i in range(200)]
    assert decided == [tel.capture_decision(f"run_{i}") for i in range(200)]
    assert 60 < sum(decided) < 140
    tel.configure()


# (why, deployment, metrics endpoint, whether startup refuses)
STARTS = [
    ("a laptop needs no metrics", "local", "", False),
    ("a deployment with metrics starts", "production", "http://127.0.0.1:9/v1/metrics", False),
    ("a deployment without metrics refuses", "production", "", True),
]


@pytest.mark.parametrize(
    ("why", "deployment", "endpoint", "refuses"), STARTS, ids=[r[0] for r in STARTS]
)
@pytest.mark.discharges("AHC-0111")
def test_a_deployment_whose_metrics_go_nowhere_does_not_start(
    monkeypatch: pytest.MonkeyPatch, why: str, deployment: str, endpoint: str, refuses: bool
) -> None:
    from scripts import run_server

    monkeypatch.setenv("AGENT_DEPLOYMENT", deployment)
    monkeypatch.setenv("AGENT_METRICS_ENDPOINT", endpoint)
    monkeypatch.setenv("AGENT_OTLP_ENDPOINT", "")
    if refuses:
        with pytest.raises(SystemExit):
            run_server._telemetry(None)
    else:
        run_server._telemetry(None)
        assert tel.exporting_metrics() is bool(endpoint)
    tel.configure()


# --------------------------------------------------------------------------- #
# The record and the watch (T-057, AHC-0114, AAC-0014, AAC-0115).
# --------------------------------------------------------------------------- #

CONTRADICTS = [
    ModelResponse(tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10002"}),)),
    ModelResponse(text="Good news: AB-10002 has shipped and is on its way to you."),
]


@pytest.mark.discharges("AHC-0114", "AAC-0060")
def test_a_turn_is_rebuilt_whole_from_its_spans() -> None:
    exporter = tel.configure(capture_payloads=True)
    with app_with(LOOKUP) as client:
        chat(client, "I have a question about AB-10002")
    (turn,) = record.turns(record.from_spans(exporter.get_finished_spans()))
    assert (turn.user_id, turn.result, turn.captured, turn.synthetic) == (
        "C-1042",
        "completed",
        True,
        False,
    )
    assert turn.input == "I have a question about AB-10002"
    assert turn.reply == "AB-10002 is pending and has not shipped yet."
    (use,) = turn.tools
    assert (use.name, use.outcome, use.arguments) == ("get_order", "ok", {"id": "AB-10002"})
    assert use.result["status"] == "pending"
    tel.configure()


@pytest.mark.discharges("AAC-0014", "AAC-0110", "AHC-0114")
def test_the_watch_finds_a_reply_that_contradicts_the_store_and_counts_it() -> None:
    exporter = tel.configure(capture_payloads=True)
    with app_with(CONTRADICTS) as client:
        chat(client, "I have a question about AB-10002")
    kept = Kept()
    report = Watch(Spans(exporter), kept, settle_s=0).once(time.time() + 1)

    assert [f.rule for f in report.findings] == ["W-01"]
    assert "the store said pending" in report.findings[0].detail
    assert [n for _, n in kept.scored] == [1]
    assert total("agent.online.findings", rule="W-01", severity="page") == 1
    assert total("agent.online.evaluated", captured="true") == 1
    tel.configure()


@pytest.mark.discharges("AHC-0028")
def test_a_score_names_the_rules_that_made_it_when_they_are_injected() -> None:
    """F-069's second half. `Watch.rules` is injectable, and every score was
    written with the module's RULES_VERSION whatever rules ran."""
    from support_agent.watch import RULES_VERSION, rules_version
    from support_agent.watch.rules import RULES

    exporter = tel.configure(capture_payloads=True)
    with app_with(CONTRADICTS) as client:
        chat(client, "I have a question about AB-10002")
    kept, only = Kept(), RULES[:1]
    Watch(Spans(exporter), kept, rules=only, settle_s=0).once(time.time() + 1)

    assert kept.versions == {rules_version(only)} != {RULES_VERSION}
    tel.configure()


# (why, the body, the customer, the status, whether an outcome follows)
FEEDBACK = [
    ("their own conversation, down", {"value": "down"}, "C-1042", 202, True),
    ("a value that is not one", {"value": "meh"}, "C-1042", 400, False),
    ("someone else's conversation", {"value": "up"}, "C-9999", 404, False),
]


@pytest.mark.parametrize(
    ("why", "body", "who", "status", "lands"), FEEDBACK, ids=[r[0] for r in FEEDBACK]
)
@pytest.mark.discharges("AHC-0112", "AAC-0115")
def test_feedback_is_the_customers_verdict_on_their_own_conversation(
    why: str, body: dict[str, str], who: str, status: int, lands: bool
) -> None:
    exporter = tel.configure(capture_payloads=True)
    with app_with(LOOKUP) as client:
        cid = chat(client, "I have a question about AB-10002").json()["conversation_id"]
        answered = client.post(
            "/feedback",
            json={"conversation_id": cid, **body},
            headers={"authorization": f"Bearer {token(who)}"},
        )
    assert answered.status_code == status
    kept = Kept()
    Watch(Spans(exporter), kept, settle_s=0).once(time.time() + 1)
    assert [o.kind for o in kept.outcomes] == ([f"feedback_{body['value']}"] if lands else [])
    tel.configure()


@pytest.mark.discharges("AAC-0115")
def test_asking_for_a_person_right_after_an_answer_is_an_outcome() -> None:
    exporter = tel.configure()
    with app_with(LOOKUP) as client:
        cid = chat(client, "I have a question about AB-10002").json()["conversation_id"]
        chat(client, "connect me to a human", cid=cid)
    kept = Kept()
    Watch(Spans(exporter), kept, settle_s=0).once(time.time() + 1)
    assert [o.kind for o in kept.outcomes] == ["asked_for_person"]
    assert total("agent.outcomes", kind="asked_for_person", source="inferred") == 1
    tel.configure()


# --------------------------------------------------------------------------- #
# The canary (T-056, AHC-0113, AAC-0116).
# --------------------------------------------------------------------------- #

CANARY_SCRIPT = [
    ModelResponse(tool_calls=(ToolCall(id="c1", name="list_orders", arguments={}),)),
    ModelResponse(text="You have two orders: CN-70001 (pending) and CN-70002 (delivered)."),
    ModelResponse(
        tool_calls=(ToolCall(id="c2", name="open_return_request", arguments={"id": "CN-70002"}),)
    ),
    ModelResponse(text="CN-70002 is outside the return window, so I cannot open a return."),
]


@pytest.mark.discharges("AHC-0113", "AAC-0116")
def test_the_canary_passes_through_the_real_edge_as_its_own_customer() -> None:
    tel.configure()
    with app_with(CANARY_SCRIPT, synthetic=frozenset({"C-7001"})) as client:

        def post(text: str, bearer: str) -> tuple[int, dict[str, Any]]:
            answered = client.post(
                "/chat", json={"text": text}, headers={"authorization": f"Bearer {bearer}"}
            )
            return answered.status_code, answered.json()

        results = canary.Canary(post, lambda: token("C-7001")).once()

    passed = {r.case: r.passed for r in results}
    # F-047: the projected world answers an order that is not yours with a tool
    # error, which the direct route turns into a 502, where the real store
    # answers `found: false`. Nothing leaks either way; the canary is right to
    # fail it, and against Saleor it passes.
    assert passed == {c.name: c.name != "someone else's order" for c in canary.CASES}, [
        r.reason for r in results
    ]
    assert results[-1].reason.startswith("HTTP 502")
    assert total("agent.canary.cases", outcome="pass") == len(canary.CASES) - 1
    assert total("agent.turns", synthetic="false") == 0


# (why, the case, the status and body the edge answered, whether it passes)
VERDICTS = [
    ("status reported", "status", 200, {"reply": "CN-70002 was delivered on Monday."}, True),
    ("status wrong", "status", 200, {"reply": "CN-70002 is on its way."}, False),
    ("edge down", "status", 502, {"error": "upstream"}, False),
    ("orders named", "orders", 200, {"reply": "You have CN-70001."}, True),
    ("orders missing", "orders", 200, {"reply": "I could not find any."}, False),
    (
        "a return claimed",
        "return outside the window",
        200,
        {"reply": "Your return has been opened."},
        False,
    ),
    ("another's order leaked", "someone else's order", 200, {"reply": "5 Park Street"}, False),
    ("another's order refused", "someone else's order", 200, {"reply": "Not found."}, True),
    ("turned away at the door", "someone else's order", 401, {"error": "not valid"}, False),
    # F-062: the direct route described an order the store said is not there.
    (
        "a missing order given a status",
        "someone else's order",
        200,
        {"reply": "Order AB-10003 is currently unknown."},
        False,
    ),
    (
        "a missing order given no refund",
        "someone else's order",
        200,
        {"reply": "There is no refund on order AB-10003."},
        False,
    ),
]


@pytest.mark.parametrize(
    ("why", "name", "status", "body", "passes"), VERDICTS, ids=[r[0] for r in VERDICTS]
)
@pytest.mark.discharges("AAC-0116")
def test_each_canary_case_knows_a_wrong_answer(
    why: str, name: str, status: int, body: dict[str, Any], passes: bool
) -> None:
    case = next(c for c in canary.CASES if c.name == name)
    assert (case.expect(status, body) is None) is passes


# --------------------------------------------------------------------------- #
# The tool surface, as numbers (T-058: AACP-0004, 0023, 0024, 0026).
# --------------------------------------------------------------------------- #

# (why, tool, arguments, result bound, the metric, its labels, how many)
SURFACE = [
    ("a tool not on the surface", "refund_everything", {}, 8000,
     "agent.tool.calls", {"tool": "unoffered", "outcome": "unknown"}, 1),
    ("arguments that fail the schema", "get_order", {"order": "AB-10002"}, 8000,
     "agent.tool.invalid_arguments", {"tool": "get_order"}, 1),
    ("a result cut to fit", "list_orders", {}, 40,
     "agent.tool.truncated", {"tool": "list_orders"}, 1),
    ("a result that fits is not cut", "get_order", {"id": "AB-10002"}, 8000,
     "agent.tool.truncated", {"tool": "get_order"}, 0),
    ("every tool listed is counted as offered", "get_order", {"id": "AB-10002"}, 8000,
     "agent.tools.offered", {"tool": "change_address"}, 1),
]  # fmt: skip


@pytest.mark.parametrize(
    ("why", "name", "arguments", "bound", "metric", "labels", "count"),
    SURFACE,
    ids=[r[0] for r in SURFACE],
)
@pytest.mark.discharges("AHC-0111", "AAC-0051", "AAC-0052", "AAC-0103", "AAC-0105")
async def test_the_tool_surface_counts_what_the_model_is_shown_and_gets_wrong(
    why: str,
    name: str,
    arguments: dict[str, Any],
    bound: int,
    metric: str,
    labels: dict[str, str],
    count: int,
) -> None:
    from support_agent import identity as ident
    from support_agent.contracts import IdempotencyKey, Identity, RunId

    tel.configure()
    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
    key = IdempotencyKey(run_id=RunId("run_surface"), step=0, iteration=0)
    async with connect(
        project(Live.start(load(WORLD))), requests=InMemoryRequests(), max_result_chars=bound
    ) as tools:
        # An unoffered tool and a bad schema both raise; the count is the point.
        with contextlib.suppress(Exception):
            await tools.call(name, arguments, who, key)
    assert total(metric, **labels) == count


# (why, the configuration's fingerprint, the label the turn's numbers carry)
CONFIGS = [
    ("a configured agent", "f1e2d3c4b5a69788", "f1e2d3c4b5a69788"),
    ("no configuration", None, "none"),
]


@pytest.mark.parametrize(("why", "fingerprint", "label"), CONFIGS, ids=[r[0] for r in CONFIGS])
@pytest.mark.discharges("AHC-0111", "AAC-0013", "AAC-0101", "AAC-0103")
def test_a_turns_numbers_say_which_configuration_produced_them(
    why: str, fingerprint: str | None, label: str
) -> None:
    """T-058, AACP-0002 and AACP-0055: a release that moved the rates or grew
    the prompt is found by comparing configurations, so each number says which
    configuration it was counted under — and stops saying it when the turn ends."""
    from support_agent.contracts import Completed, Direct, Intent
    from support_agent.telemetry import counters

    tel.configure()
    with tel.turn_scope("run_cfg", fingerprint):
        counters.record_turn(
            Completed(reply="ok"), Direct(intent=Intent.ORDER_STATUS, handler="get_order")
        )
        counters.token_usage.record(
            10, {"gen_ai.token.type": "input", "config": counters.configuration()}
        )
    assert total("agent.turns", config=label) == 1
    assert total("gen_ai.client.token.usage", config=label) == 1
    assert counters.configuration() == counters.UNCONFIGURED


@pytest.mark.discharges("AHC-0114", "AAC-0042", "AAC-0012")
def test_the_record_carries_a_turns_tokens_and_its_models() -> None:
    """T-058, AACP-0003 and AACP-0054: what a turn paid to resend its history,
    and which model each call asked for and got — read from the record, never
    re-derived by the rule."""
    exporter = tel.configure()
    with tel.span("agent.turn", **{tel.RUN_ID: "run_m", tel.SESSION_ID: "cnv_m"}):
        for asked, got, read in (("groq/a", "a", 300), ("groq/a", "b", 500)):
            with tel.span(
                "gen_ai.chat",
                **{tel.GEN_AI_PROVIDER: "groq", tel.GEN_AI_REQUEST_MODEL: asked},
            ) as call:
                call.set_attribute(tel.GEN_AI_RESPONSE_MODEL, got)
                tel.set_usage(call, input_tokens=read, output_tokens=1)
    (turn,) = record.turns(record.from_spans(exporter.get_finished_spans()))
    assert (turn.input_tokens, turn.models) == (800, (("groq/a", "a"), ("groq/a", "b")))
    exporter.clear()
