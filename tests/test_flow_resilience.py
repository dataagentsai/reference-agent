"""Under load, and when things break."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import flow as flw
from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import resilience as res
from support_agent import telemetry as tel
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    ToolCall,
)
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient, retry_after_of
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT, connect


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def customer(extra: frozenset[str] = frozenset()) -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES | extra)


# --------------------------------------------------------------------------- #
# Fan-out.
# --------------------------------------------------------------------------- #


async def test_results_come_back_in_the_order_they_were_asked_for() -> None:
    """Not a nicety. Tool results are matched to calls by position, so a
    scheduler returning them as they finish attaches each answer to the wrong
    question."""

    async def slow(n: int) -> int:
        await asyncio.sleep((5 - n) * 0.01)
        return n

    out = await flw.gather_bounded([(lambda n=n: slow(n)) for n in range(5)], limit=5)
    assert out == [0, 1, 2, 3, 4]


async def test_concurrency_is_actually_bounded() -> None:
    limiter = flw.Limiter(limit=2)

    async def work() -> None:
        await asyncio.sleep(0.02)

    await flw.gather_bounded([work] * 6, limiter=limiter)
    assert limiter.peak <= 2


async def test_an_empty_batch_is_not_an_error() -> None:
    assert await flw.gather_bounded([]) == []


def test_a_zero_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        flw.Limiter(limit=0)


# --------------------------------------------------------------------------- #
# Throttling.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0021")
def test_the_provider_instruction_is_used_rather_than_a_guess() -> None:
    throttle = res.Throttle()
    throttle.note_retry_after(2.5, now=100.0)
    assert throttle.delay(now=100.0) == pytest.approx(2.5)
    assert throttle.delay(now=103.0) == 0


@pytest.mark.discharges("AHC-0021")
def test_the_longest_instruction_wins() -> None:
    """One caller learning of a five-second limit must not be overwritten by
    another that only heard one second."""
    throttle = res.Throttle()
    throttle.note_retry_after(5.0, now=100.0)
    throttle.note_retry_after(1.0, now=100.0)
    assert throttle.delay(now=100.0) == pytest.approx(5.0)


RETRY_AFTER_CASES = [
    ("plain seconds", {"retry-after": "3"}, 3.0),
    ("capitalised", {"Retry-After": "7"}, 7.0),
    ("unparseable", {"retry-after": "next tuesday"}, None),
    ("absent", {}, None),
]


@pytest.mark.discharges("AHC-0021")
@pytest.mark.parametrize(
    ("name", "headers", "expected"), RETRY_AFTER_CASES, ids=[c[0] for c in RETRY_AFTER_CASES]
)
def test_reading_retry_after(name: str, headers: dict, expected: float | None) -> None:
    """`None` rather than a guess. Inventing a backoff when the provider offered
    a real one is choosing to be wrong on purpose."""

    class Response:
        def __init__(self, h):
            self.headers = h

    class Error(Exception):
        def __init__(self, h):
            self.response = Response(h)

    assert retry_after_of(Error(headers)) == expected


@pytest.mark.discharges("AHC-0021")
def test_an_error_with_no_response_yields_nothing() -> None:
    assert retry_after_of(RuntimeError("plain")) is None


# --------------------------------------------------------------------------- #
# Retry.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0009", "AHC-0024")
async def test_a_transient_failure_succeeds_on_a_later_attempt() -> None:
    calls: list[int] = []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise res.Retryable("not yet")
        return "ok"

    assert await asyncio.wait_for(res.with_retry(flaky, sleep=_no_sleep), timeout=1) == "ok"
    assert len(calls) == 3


@pytest.mark.discharges("AHC-0005")
async def test_a_non_retryable_error_is_not_retried() -> None:
    calls: list[int] = []

    async def broken() -> str:
        calls.append(1)
        raise ValueError("this is a bug, not a blip")

    with pytest.raises(ValueError):
        await res.with_retry(broken, sleep=_no_sleep)
    assert len(calls) == 1


@pytest.mark.discharges("AHC-0005", "B11")
async def test_giving_up_re_raises_the_last_error() -> None:
    """A helper that swallows the final error leaves the caller unable to tell
    "it worked" from "we stopped asking"."""

    async def always() -> str:
        raise res.Retryable("still down")

    with pytest.raises(res.Retryable, match="still down"):
        await res.with_retry(always, attempts=2, sleep=_no_sleep)


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.discharges("AHC-0024", "AHC-0021")
def test_backoff_grows_and_is_capped() -> None:
    policy = res.Backoff(base_s=1.0, factor=2.0, max_s=4.0, jitter=0.0)
    assert [policy.delay(i, rand=lambda: 0.5) for i in range(5)] == [1.0, 2.0, 4.0, 4.0, 4.0]


@pytest.mark.discharges("AHC-0021")
def test_jitter_spreads_the_herd() -> None:
    """Without it, every client that failed at the same moment retries at the
    same moment and knocks over the dependency that was recovering."""
    policy = res.Backoff(base_s=1.0, jitter=0.5)
    assert policy.delay(0, rand=lambda: 0.0) < policy.delay(0, rand=lambda: 1.0)


# --------------------------------------------------------------------------- #
# The breaker.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0005")
def test_the_breaker_opens_after_the_threshold() -> None:
    breaker = res.CircuitBreaker(threshold=3, now=lambda: 0.0)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.closed
    breaker.record_failure()
    assert not breaker.closed


@pytest.mark.discharges("AHC-0005")
def test_one_probe_is_let_through_after_the_cooldown() -> None:
    """Several would re-open the breaker on a dependency that is only half
    recovered."""
    clock = [0.0]
    breaker = res.CircuitBreaker(threshold=1, cooldown_s=10.0, now=lambda: clock[0])
    breaker.record_failure()
    assert breaker.state is res.BreakerState.OPEN
    clock[0] = 11.0
    assert breaker.state is res.BreakerState.HALF_OPEN


@pytest.mark.discharges("AHC-0005")
async def test_an_open_breaker_fails_fast_instead_of_calling() -> None:
    called: list[int] = []

    async def work() -> str:
        called.append(1)
        return "should not happen"

    breaker = res.CircuitBreaker(threshold=1, cooldown_s=999, now=lambda: 0.0)
    breaker.record_failure()
    with pytest.raises(res.Retryable, match="circuit is open"):
        await breaker.call(work)
    assert called == []


@pytest.mark.discharges("AHC-0005")
async def test_a_success_closes_the_breaker_again() -> None:
    breaker = res.CircuitBreaker(threshold=2, now=lambda: 0.0)
    breaker.record_failure()

    async def works() -> str:
        return "fine"

    assert await breaker.call(works) == "fine"
    breaker.record_failure()
    assert breaker.closed


# --------------------------------------------------------------------------- #
# Compensation. AHC-0058 — declared, never inferred.
# --------------------------------------------------------------------------- #

COMPENSABLE = [
    ("refund", "issue_refund", "reverse_refund"),
    ("cancellation", "cancel_order", "reinstate_order"),
    ("return request", "open_return_request", "close_return_request"),
]


@pytest.mark.parametrize(("name", "action", "undo"), COMPENSABLE, ids=[c[0] for c in COMPENSABLE])
@pytest.mark.discharges("AHC-0058")
def test_declared_compensations(name: str, action: str, undo: str) -> None:
    assert res.compensation_for(action).undo_action == undo


def test_an_undeclared_action_raises_rather_than_returning_none() -> None:
    """A caller treating "no compensation" as "nothing to do" has silently
    decided an irreversible action was reversible."""
    with pytest.raises(res.NoCompensation, match="cannot be undone"):
        res.compensation_for("dispatch_replacement")


@pytest.mark.discharges("AHC-0058")
def test_compensation_is_not_the_same_thing_as_idempotency() -> None:
    """A perfectly deduplicated refund that should never have been issued is
    still a refund that has to be reversed."""
    assert res.is_compensable("issue_refund")
    assert res.compensation_for("issue_refund").undo_action != "issue_refund"


# --------------------------------------------------------------------------- #
# Through the loop: reads in parallel, writes in order.
# --------------------------------------------------------------------------- #


class Out(BaseModel):
    order_id: str
    status: str


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"order": [], "cancel": []}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    async def get_order(order_id: str) -> Out:
        """Look up an order."""
        srv.state["order"].append(("start", order_id))  # type: ignore[attr-defined]
        await asyncio.sleep(0.03)
        srv.state["order"].append(("end", order_id))  # type: ignore[attr-defined]
        return Out(order_id=order_id, status="shipped")

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_ORDERS_WRITE,
        }
    )
    async def cancel_order(order_id: str) -> Out:
        """Cancel an order."""
        srv.state["cancel"].append(("start", order_id))  # type: ignore[attr-defined]
        await asyncio.sleep(0.03)
        srv.state["cancel"].append(("end", order_id))  # type: ignore[attr-defined]
        return Out(order_id=order_id, status="cancelled")

    return srv


def batch(name: str, *ids: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=tuple(
            ToolCall(id=f"tc{i}", name=name, arguments={"order_id": o}) for i, o in enumerate(ids)
        )
    )


async def test_parallel_reads_interleave(server) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        await agent_loop.run(
            "check three orders",
            identity=customer(),
            llm=ScriptedClient(
                [batch("get_order", "A-1", "A-2", "A-3"), ModelResponse(text="done")]
            ),
            tools=tools,
            system_prompt="s",
        )
    events = server.state["order"]
    assert events[:3] == [("start", "A-1"), ("start", "A-2"), ("start", "A-3")]


async def test_writes_run_one_at_a_time_in_the_order_asked(server) -> None:
    """A write that fails halfway through a parallel batch costs a
    reconciliation in an order that depended on scheduling."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        await agent_loop.run(
            "cancel three orders",
            identity=customer(),
            llm=ScriptedClient(
                [batch("cancel_order", "A-1", "A-2", "A-3"), ModelResponse(text="done")]
            ),
            tools=tools,
            system_prompt="s",
        )
    assert server.state["cancel"] == [
        ("start", "A-1"),
        ("end", "A-1"),
        ("start", "A-2"),
        ("end", "A-2"),
        ("start", "A-3"),
        ("end", "A-3"),
    ]


async def test_results_stay_matched_to_their_calls(server) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        _, trace = await agent_loop.run(
            "check three",
            identity=customer(),
            llm=ScriptedClient([batch("get_order", "A-1", "A-2", "A-3"), ModelResponse(text="ok")]),
            tools=tools,
            system_prompt="s",
        )
    assert [args for _, args in trace.tool_calls] == [
        '{"order_id": "A-1"}',
        '{"order_id": "A-2"}',
        '{"order_id": "A-3"}',
    ]
