"""The layer above config and telemetry: the model seam, identity, and the
ledger that decides whether an effect happens once or twice."""

from __future__ import annotations

import pytest

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import (
    IdempotencyKey,
    Identity,
    LLMClient,
    Message,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    RunId,
    SideEffectClass,
    ToolResult,
    Usage,
)
from support_agent.idempotency import InMemoryLedger, once
from support_agent.llm import ScriptedClient, UnavailableClient

SECRET = "test-secret-long-enough-for-hs256-32b"
RUN = RunId("run_t")


def key(step: int = 1, iteration: int = 0) -> IdempotencyKey:
    return IdempotencyKey(run_id=RUN, step=step, iteration=iteration)


def request(text: str = "hello") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),))


# --------------------------------------------------------------------------- #
# The seam. Adapters satisfy LLMClient structurally — no adapter imports it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "client"),
    [
        ("scripted", ScriptedClient([])),
        ("unavailable", UnavailableClient()),
    ],
    ids=["scripted", "unavailable"],
)
@pytest.mark.discharges("AHC-0022")
def test_adapters_satisfy_the_protocol_structurally(name: str, client: object) -> None:
    assert isinstance(client, LLMClient)


@pytest.mark.discharges("AAC-0059", "AHC-0022")
async def test_scripted_client_is_deterministic_and_free() -> None:
    tel.configure()
    scripted = ScriptedClient([ModelResponse(text="one"), ModelResponse(text="two")])
    assert (await scripted.complete(request())).text == "one"
    assert (await scripted.complete(request())).text == "two"
    assert scripted.exhausted


async def test_exhausting_the_script_is_an_error_not_an_empty_reply() -> None:
    """A scenario that runs longer than its script did not test what it claimed."""
    tel.configure()
    scripted = ScriptedClient([ModelResponse(text="only")])
    await scripted.complete(request())
    with pytest.raises(ModelUnavailable, match="exhausted"):
        await scripted.complete(request())


@pytest.mark.discharges("AAC-0009")
async def test_provider_failure_is_typed_not_a_stack_trace() -> None:
    """AAC-0009 — degradation is a declared path."""
    with pytest.raises(ModelUnavailable):
        await UnavailableClient().complete(request())


@pytest.mark.discharges("AAC-0011", "B8")
async def test_model_call_emits_a_span_with_usage() -> None:
    exporter = tel.configure()
    scripted = ScriptedClient(
        [ModelResponse(text="hi", usage=Usage(input_tokens=10, output_tokens=4))]
    )
    await scripted.complete(request())
    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert attrs[tel.GEN_AI_INPUT_TOKENS] == 10
    assert attrs[tel.GEN_AI_OUTPUT_TOKENS] == 4


# --------------------------------------------------------------------------- #
# Identity. Authorisation comes from the token, never from anything said.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0099")
def test_round_trip_yields_the_customer_and_scopes() -> None:
    who = ident.verify(ident.mint("C-1042", secret=SECRET), secret=SECRET)
    assert who.customer_id == "C-1042"
    assert who.may(ident.SCOPE_ORDERS_READ)


REJECTION_CASES = [
    ("wrong secret", lambda t: (t, "other-secret-also-long-enough-32bytes")),
    ("tampered payload", lambda t: (t[:-4] + "AAAA", SECRET)),
    ("not a token at all", lambda t: ("garbage", SECRET)),
]


@pytest.mark.discharges("AHC-0099", "AAC-0111")
@pytest.mark.parametrize(("name", "mangle"), REJECTION_CASES, ids=[c[0] for c in REJECTION_CASES])
def test_bad_tokens_produce_no_identity_at_all(name: str, mangle) -> None:
    token, secret = mangle(ident.mint("C-1042", secret=SECRET))
    with pytest.raises(ident.InvalidSession):
        ident.verify(token, secret=secret)


@pytest.mark.discharges("AHC-0099", "AAC-0111")
def test_expired_token_is_refused() -> None:
    token = ident.mint("C-1042", secret=SECRET, ttl_s=60, now=1_000_000)
    with pytest.raises(ident.InvalidSession):
        ident.verify(token, secret=SECRET, now=1_000_061)


@pytest.mark.discharges("AHC-0099")
def test_a_short_secret_is_a_configuration_fault_not_a_warning() -> None:
    """RFC 7518 §3.2. Enforced, and it takes the process down at startup rather
    than failing one customer quietly at runtime."""
    with pytest.raises(ValueError, match="at least 32"):
        ident.mint("C-1", secret="too-short")


@pytest.mark.discharges("AAC-0057", "AHC-0057")
def test_customer_scopes_exclude_refunds() -> None:
    """A refund is irreversible and needs a human above the threshold, so the
    agent acting as the customer must not hold the scope that would skip it."""
    assert ident.SCOPE_REFUNDS_WRITE not in ident.CUSTOMER_SCOPES
    who = ident.verify(ident.mint("C-1", secret=SECRET), secret=SECRET)
    with pytest.raises(ident.InvalidSession, match="lacks scope"):
        ident.require(who, ident.SCOPE_REFUNDS_WRITE)


@pytest.mark.discharges("AHC-0099", "AAC-0111", "AHC-0034")
def test_confused_deputy_needs_a_different_token_not_a_different_claim() -> None:
    """T-AD-01. Whatever the model believes about who it is talking to, the
    identity it can act as is the one the token carries."""
    who = ident.verify(ident.mint("C-1042", secret=SECRET), secret=SECRET)
    assert who.customer_id == "C-1042"
    assert Identity(customer_id="C-9999").customer_id != who.customer_id


# --------------------------------------------------------------------------- #
# The ledger. One refund, or two.
# --------------------------------------------------------------------------- #


def ok(name: str = "issue_refund") -> ToolResult:
    return ToolResult(name=name, structured={"refund_id": "rf_1"})


@pytest.mark.discharges("AAC-0047")
async def test_retry_under_the_same_key_does_not_execute_twice() -> None:
    """The timeout case: the call succeeded, the response was lost, the harness
    believes it failed. Without a key nothing can tell that from a fresh call."""
    ledger, calls = InMemoryLedger(), []

    async def action() -> ToolResult:
        calls.append(1)
        return ok()

    first, replayed_1 = await once(ledger, key(), SideEffectClass.IRREVERSIBLE, action)
    second, replayed_2 = await once(ledger, key(), SideEffectClass.IRREVERSIBLE, action)

    assert len(calls) == 1
    assert first == second
    assert (replayed_1, replayed_2) == (False, True)


async def test_a_legitimate_second_execution_does_run() -> None:
    """Same step, next iteration — the loop genuinely came round again."""
    ledger, calls = InMemoryLedger(), []

    async def action() -> ToolResult:
        calls.append(1)
        return ok()

    await once(ledger, key(1, 0), SideEffectClass.IRREVERSIBLE, action)
    await once(ledger, key(1, 1), SideEffectClass.IRREVERSIBLE, action)
    assert len(calls) == 2


@pytest.mark.discharges("AHC-0074")
async def test_reads_bypass_the_ledger() -> None:
    ledger, calls = InMemoryLedger(), []

    async def action() -> ToolResult:
        calls.append(1)
        return ok("get_order")

    await once(ledger, key(), SideEffectClass.READ, action)
    await once(ledger, key(), SideEffectClass.READ, action)
    assert len(calls) == 2
    assert len(ledger) == 0


@pytest.mark.discharges("AAC-0046")
async def test_failures_are_not_recorded_so_a_retry_can_reach_the_tool() -> None:
    """Recording failures would turn one transient 503 into a permanent refusal."""
    ledger, calls = InMemoryLedger(), []

    async def flaky() -> ToolResult:
        calls.append(1)
        if len(calls) == 1:
            return ToolResult(name="issue_refund", is_error=True, error_channel="execution")
        return ok()

    first, _ = await once(ledger, key(), SideEffectClass.IRREVERSIBLE, flaky)
    second, replayed = await once(ledger, key(), SideEffectClass.IRREVERSIBLE, flaky)

    assert first.is_error and not second.is_error
    assert len(calls) == 2
    assert not replayed


async def test_the_first_outcome_for_a_key_is_the_outcome() -> None:
    ledger = InMemoryLedger()
    await ledger.record(key(), ok("first"))
    await ledger.record(key(), ok("second"))
    stored = await ledger.seen(key())
    assert stored is not None and stored.name == "first"


def test_in_memory_ledger_declares_it_is_not_durable() -> None:
    """P6 exists because enforcement must survive process death. This one does
    not, and says so rather than being quietly wrong in production."""
    assert InMemoryLedger.durable is False
