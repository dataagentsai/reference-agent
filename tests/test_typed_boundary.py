"""AHC-0001 — every model response crosses a typed boundary.

The capability's own failure mode: *"malformed output propagates as far as the
first component that assumes structure, and surfaces as a KeyError three layers
away from the model call that caused it."*

That was us. `_from_wire` had no error handling, so a model returning truncated
tool-call arguments — the ordinary consequence of hitting a token limit — raised
`JSONDecodeError` out of the client, past the loop's `except ModelUnavailable`,
and out of the agent as an unhandled exception.

**No test drove it**, which is why 474 passing tests did not notice. The
degenerate-input cases in `test_conformance.py` drive degenerate *customer*
input; nothing drove degenerate *model output*. AAC-0015 was discharged on one
side of the boundary only — F-005's lesson again: an obligation is only as
discharged as the narrowest case that claims it.
"""

from __future__ import annotations

from types import SimpleNamespace as N

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, ModelMalformed, ModelUnavailable, SideEffectClass
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import _from_wire
from support_agent.tools import META_SIDE_EFFECT, connect

SYSTEM = "You are a support agent."


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture
def server():
    srv = MCPServer("ecom")

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    return srv


def open_tools(server):
    return connect(server, ledger=InMemoryLedger())


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def wire(arguments: str | None, *, content: str = "", tool: str = "cancel_order"):
    """A provider response shaped like the OpenAI-compatible wire format."""
    calls = (
        [N(id="c1", function=N(name=tool, arguments=arguments))] if arguments is not None else None
    )
    return N(
        choices=[N(message=N(content=content, tool_calls=calls), finish_reason="tool_calls")],
        usage=N(prompt_tokens=10, completion_tokens=5),
        model="openai/gpt-oss-120b",
    )


# --------------------------------------------------------------------------- #
# What the boundary must let through.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "arguments", "expected"),
    [
        ("an ordinary call", '{"order_id":"AB-1"}', {"order_id": "AB-1"}),
        ("no arguments at all", "", {}),
        ("an explicitly empty object", "{}", {}),
        ("nested values survive", '{"a":{"b":[1,2]}}', {"a": {"b": [1, 2]}}),
        ("unicode is not mangled", '{"note":"café ☕"}', {"note": "café ☕"}),
    ],
)
@pytest.mark.discharges("AHC-0001")
def test_a_well_formed_response_parses(why: str, arguments: str, expected: dict) -> None:
    parsed = _from_wire(wire(arguments))
    assert parsed.tool_calls[0].arguments == expected, why
    assert parsed.model == "openai/gpt-oss-120b"
    assert parsed.usage.input_tokens == 10


@pytest.mark.discharges("AHC-0001")
def test_a_plain_text_reply_needs_no_tool_calls() -> None:
    parsed = _from_wire(wire(None, content="Your order has shipped."))
    assert parsed.text == "Your order has shipped."
    assert parsed.tool_calls == ()
    assert not parsed.wants_tools


# --------------------------------------------------------------------------- #
# What it must turn into a typed failure. Every row raised an untyped
# exception before this file existed.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "raw"),
    [
        # The common one, and the reason this matters: a token limit cuts the
        # JSON mid-object. Not exotic — it is what a max_tokens ceiling does.
        ("truncated mid-object", wire('{"order_id":')),
        ("truncated mid-string", wire('{"order_id":"AB-')),
        ("prose instead of JSON", wire("order AB-1 please")),
        ("a fenced code block", wire('```json\n{"order_id":"AB-1"}\n```')),
        # Valid JSON, and still not a call. Without an explicit check this
        # arrives one layer up as a failure about the tool rather than the
        # model, sending whoever debugs it to the wrong file.
        ("a JSON string, not an object", wire('"AB-1"')),
        ("a JSON array", wire('["AB-1"]')),
        ("a JSON number", wire("42")),
        ("a JSON null", wire("null")),
        # Shape failures above the arguments.
        ("no choices", N(choices=[], usage=N(), model="m")),
        ("no choices attribute at all", N(usage=N(), model="m")),
    ],
)
@pytest.mark.discharges("AHC-0001", "AHC-0005", "B11")
def test_a_malformed_response_becomes_a_typed_failure(why: str, raw: object) -> None:
    with pytest.raises(ModelMalformed) as raised:
        _from_wire(raw)
    assert raised.value.reason, f"{why}: a failure nobody can read is not much better"


@pytest.mark.discharges("AHC-0005", "B11")
def test_malformed_is_not_unavailable() -> None:
    """Deliberately separate types, because the right response differs.

    Unavailable means nothing came back and a retry may work. Malformed means
    this model, on this prompt, produced something unusable — retrying the
    identical request is the least likely thing to help and costs a second bill.
    """
    assert not issubclass(ModelMalformed, ModelUnavailable)
    assert not issubclass(ModelUnavailable, ModelMalformed)


@pytest.mark.discharges("AHC-0001", "AHC-0017")
def test_the_failure_carries_the_raw_text_for_the_operator() -> None:
    """AHC-0001's `raw_retention` tension, resolved toward explicability.

    Held on the exception in memory, redacted by `telemetry` before it is
    recorded, and never persisted — so a failure can be explained afterwards
    without putting model output that may echo sensitive input into storage.
    """
    with pytest.raises(ModelMalformed) as raised:
        _from_wire(wire('{"order_id":'))

    assert raised.value.raw == '{"order_id":'
    assert "cancel_order" in raised.value.reason, "name the tool, not just the error"


# --------------------------------------------------------------------------- #
# The half the requirement is actually about: a caller HANDLES it.
#
# "The failure is a declared return shape that callers must handle, not an
# exception raised from wherever the parse happened to fail."
# --------------------------------------------------------------------------- #


class MalformedClient:
    """A provider that answers, unreadably. `mock` resolution, no network."""

    def __init__(self, reason: str = "tool call 'cancel_order' has unreadable arguments") -> None:
        self.reason = reason
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        raise ModelMalformed(self.reason, raw='{"order_id":')


@pytest.mark.discharges("AAC-0002", "AAC-0015", "AAC-0009", "AHC-0001", "AHC-0005", "AHC-0017")
async def test_the_loop_degrades_instead_of_crashing(server) -> None:
    """Before the fix this test raised `JSONDecodeError` out of `agent_loop.run`.

    Now it returns the same declared shape a provider outage does — the customer
    gets a sentence, the operator gets the detail, and nothing escapes as a
    stack trace.
    """
    from support_agent import loop as agent_loop
    from support_agent.contracts import Failed

    llm = MalformedClient()
    async with open_tools(server) as tools:
        result, trace = await agent_loop.run(
            "cancel my order AB-1",
            identity=customer(),
            llm=llm,
            tools=tools,
            system_prompt=SYSTEM,
        )

    assert isinstance(result, Failed)
    assert result.customer_message and "unreadable" not in result.customer_message, (
        "the customer gets a sentence, not the parser's opinion"
    )
    assert "unreadable" in result.detail, "the operator gets the detail"


@pytest.mark.discharges("AAC-0099", "AHC-0001")
async def test_the_failures_are_counted_not_merely_survived(server) -> None:
    """AHC-0001's `parse_failure` decision: *fail into a declared shape and count
    the failures.* Surviving quietly is how a model swap silently degrades — the
    agent keeps answering, a rising share of runs fail to parse, and no number
    anywhere moves."""
    from support_agent import loop as agent_loop

    async with open_tools(server) as tools:
        _, trace = await agent_loop.run(
            "cancel my order AB-1",
            identity=customer(),
            llm=MalformedClient(),
            tools=tools,
            system_prompt=SYSTEM,
        )

    assert trace.malformed == 1


@pytest.mark.discharges("AHC-0001", "AHC-0005")
async def test_a_malformed_response_is_not_retried(server) -> None:
    """The other half of that decision, asserted so it cannot drift.

    Unavailable may pass on a second attempt. Malformed means the same prompt to
    the same model produced something unusable, so a retry mostly buys a second
    bill — and a retry loop here would turn one bad response into `max_steps`
    of them.
    """
    from support_agent import loop as agent_loop

    llm = MalformedClient()
    async with open_tools(server) as tools:
        await agent_loop.run(
            "cancel my order AB-1",
            identity=customer(),
            llm=llm,
            tools=tools,
            system_prompt=SYSTEM,
        )

    assert llm.calls == 1, f"called the provider {llm.calls} times for one unreadable answer"


EMPTY = [
    ("nothing at all, budget spent", "", "length", 2048),
    ("nothing at all, stopped normally", "", "stop", 40),
    ("whitespace is nothing", "   \n  ", "stop", 12),
]


@pytest.mark.discharges("AHC-0001", "AHC-0025")
@pytest.mark.parametrize(("name", "content", "reason", "spent"), EMPTY, ids=[e[0] for e in EMPTY])
def test_a_completion_with_no_answer_in_it_is_malformed(
    name: str, content: str, reason: str, spent: int
) -> None:
    """F-031. A reasoning model spends the output budget thinking and returns
    empty content when it runs out — billed, and with nothing in it.

    It used to become `ModelResponse(text="")`, which the loop reads as *the
    model chose to answer and had nothing to say*: the customer gets an empty
    reply and the run records success. It is a malformed completion and takes
    the declared path, where it is counted and never retried.
    """
    from support_agent.llm import _from_wire

    class Message:
        tool_calls: list = []

    class Choice:
        message = Message()
        finish_reason = reason

    class Usage:
        prompt_tokens, completion_tokens, total_tokens = 10, spent, 10 + spent

    class Raw:
        choices = [Choice()]
        model = "openai/gpt-oss-120b"
        usage = Usage()

    Message.content = content  # type: ignore[attr-defined]
    with pytest.raises(ModelMalformed, match="neither text nor a tool call"):
        _from_wire(Raw())
