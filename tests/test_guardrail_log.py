"""claims-fnol-azure A9 — a gateway's Content Safety verdict, recorded as a check result.

Three tables, no network:

1. `guardrail_log` over a reply's recorded verdicts: pass, a block with its
   category, Content Safety unavailable, and no verdict at all (locally, no APIM).
2. Each model client carries the gateway's header onto the `ModelResponse` it
   returns and onto its span — the refused call's too, where there is no response.
3. The verdict survives the trip through the trace: span → watch turn →
   the `online` position's request.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from agent_harness import telemetry as tel
from agent_harness.contracts import Message, ModelRefused, ModelRequest
from agent_harness.evals import EvalRequest, Response, judge
from agent_harness.evals import plan as ev
from agent_harness.evals.guardrail import GuardrailLog, verdict_of
from agent_harness.llm import GroqClient
from agent_harness.llm.gateway import SPAN_PREFIX
from agent_harness.llm.pydantic_ai import openai_compatible
from agent_harness.watch import online
from agent_harness.watch.record import Node, turns

CS = "x-content-safety"

# (why, what the gateway said about each model call, verdict, label)
VERDICTS: list[tuple[str, tuple[dict[str, str], ...], str, str]] = [
    ("pass", ({CS: "pass; prompt=pass; completion=pass"},), "pass", "pass"),
    ("block:Hate", ({CS: "block:Hate; prompt=block:Hate; completion=skipped"},), "fail", "Hate"),
    (
        "unavailable: failed open",
        ({CS: "unavailable; prompt=unavailable; completion=pass"},),
        "skip",
        "unavailable",
    ),
    ("header absent: no gateway verdict", ({},), "skip", ""),
    ("no model call recorded", (), "skip", ""),
    (
        "a later call blocked",
        ({CS: "pass; prompt=pass; completion=skipped"}, {CS: "block:Violence"}),
        "fail",
        "Violence",
    ),
    ("parts only, no overall", ({CS: "prompt=pass; completion=block:Sexual"},), "fail", "Sexual"),
    ("nothing screened", ({CS: "skipped; prompt=skipped; completion=skipped"},), "skip", ""),
]


@pytest.mark.discharges("AAC-0088", "AHC-0028")
@pytest.mark.parametrize(
    ("why", "calls", "verdict", "label"), VERDICTS, ids=[v[0] for v in VERDICTS]
)
def test_guardrail_log_records_the_gateways_verdict(
    why: str, calls: tuple[dict[str, str], ...], verdict: str, label: str
) -> None:
    result = judge(
        GuardrailLog(name="content_safety"),
        EvalRequest(response=Response(text="Your claim is registered.", gateway=calls)),
    )
    assert (result.verdict, result.label) == (verdict, label)
    assert result.provider == "gateway" and result.evaluator == "content_safety"
    if verdict == "skip" and not label:
        assert result.reason == "skip: no gateway verdict"


# (why, categories the YAML judges, the header, verdict)
CATEGORIES = [
    ("a judged category fails", ["Hate", "Violence"], "block:Hate", "fail"),
    ("an unjudged category passes, labelled", ["Hate"], "block:SelfHarm", "pass"),
    ("no list: every category fails", [], "block:SelfHarm", "fail"),
]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize(
    ("why", "categories", "header", "verdict"), CATEGORIES, ids=[c[0] for c in CATEGORIES]
)
def test_the_yaml_names_the_categories_that_fail(
    why: str, categories: list[str], header: str, verdict: str
) -> None:
    named = f"[{', '.join(categories)}]"
    plan = ev.load(
        f"evaluators: {{cs: {{kind: guardrail_log, categories: {named}}}}}\n"
        "positions: {online: {run: [{use: cs, on_fail: alert}]}}",
        rules={},
    )
    request = EvalRequest(response=Response(text="", gateway=({CS: header},)))
    assert [r.verdict for r in plan.run_online(request, key="t")] == [verdict]
    assert plan.where("cs") == ("online",)


@pytest.mark.parametrize(
    ("value", "overall"),
    [
        ("pass; prompt=pass; completion=pass", "pass"),
        ("prompt=pass; completion=unavailable", "unavailable"),
        ("block:prompt-attack", "block:prompt-attack"),
        ("prompt=skipped; completion=skipped", "skipped"),
    ],
)
@pytest.mark.discharges("AHC-0028")
def test_the_header_grammar(value: str, overall: str) -> None:
    assert verdict_of(value) == overall


# --------------------------------------------------------------------------- 2
CHAT_OK = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "openai/gpt-oss-120b",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Your claim is registered."},
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}
REFUSED = {"error": {"code": "content_safety", "message": "the prompt was blocked"}}
ASK = ModelRequest(messages=(Message(role="user", content="hello"),))


def http(status: int, body: dict[str, Any], headers: dict[str, str]) -> httpx2.AsyncClient:
    def handle(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body, headers=headers)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


def pydantic_ai(client: httpx2.AsyncClient) -> Any:
    return openai_compatible(
        api_key="apim-sub",
        base_url="https://gw.azure-api.net/groq/v1",
        model="openai/gpt-oss-120b",
        key_header="Ocp-Apim-Subscription-Key",
        http_client=client,
    )


def groq(client: httpx2.AsyncClient) -> Any:
    return GroqClient(
        api_key="apim-sub",
        base_url="https://gw.azure-api.net/groq/v1",
        model="openai/gpt-oss-120b",
        http_client=client,
    )


SAID = "pass; prompt=pass; completion=pass"
# (why, client, headers on the response, what the response's `gateway` holds)
CARRIED = [
    ("pydantic ai: the verdict", pydantic_ai, {CS: SAID}, {CS: SAID}),
    ("pydantic ai: no header", pydantic_ai, {}, {}),
    ("pydantic ai: other headers stay behind", pydantic_ai, {"x-request-id": "r1"}, {}),
    ("groq client: the verdict", groq, {CS: SAID}, {CS: SAID}),
    ("groq client: no header", groq, {}, {}),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(
    ("why", "make", "headers", "carried"), CARRIED, ids=[c[0] for c in CARRIED]
)
async def test_each_client_carries_the_gateways_verdict(
    why: str, make: Any, headers: dict[str, str], carried: dict[str, str]
) -> None:
    exporter = tel.configure()
    response = await make(http(200, CHAT_OK, headers)).complete(ASK)
    assert response.gateway == carried
    assert response.text == "Your claim is registered."
    chat = next(s for s in exporter.get_finished_spans() if s.name == "gen_ai.chat")
    assert {k: v for k, v in tel.attributes_of(chat).items() if k.startswith(SPAN_PREFIX)} == {
        SPAN_PREFIX + k: v for k, v in carried.items()
    }


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize("make", [pydantic_ai, groq], ids=["pydantic ai", "groq client"])
async def test_a_blocked_prompt_is_recorded_on_the_span_of_the_refused_call(make: Any) -> None:
    exporter = tel.configure()
    with pytest.raises(ModelRefused):
        await make(http(403, REFUSED, {CS: "block:Hate; prompt=block:Hate"})).complete(ASK)
    chat = next(s for s in exporter.get_finished_spans() if s.name == "gen_ai.chat")
    assert tel.attributes_of(chat)[SPAN_PREFIX + CS] == "block:Hate; prompt=block:Hate"


# --------------------------------------------------------------------------- 3
@pytest.mark.discharges("AHC-0114")
def test_the_verdict_reaches_the_online_position_through_the_trace() -> None:
    nodes = [
        Node("t1", "agent.turn", {tel.CAPTURED: True, tel.INPUT: "hi", tel.REPLY: "ok"}, 0, 2),
        Node("t1", "gen_ai.chat", {SPAN_PREFIX + CS: "block:Hate; prompt=block:Hate"}, 0.5, 1),
        Node("t1", "gen_ai.chat", {}, 1.2, 1.5),
    ]
    (turn,) = turns(nodes)
    assert turn.gateway == ({CS: "block:Hate; prompt=block:Hate"}, {})
    request = online.request_of(turn)
    result = judge(GuardrailLog(name="content_safety"), request)
    assert (result.verdict, result.label) == ("fail", "Hate")
