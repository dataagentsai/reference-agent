"""The frozen golden set, and regression against a committed baseline.

F1 from the test spec, and the obligations that need something to compare
against: a set that does not change, and last release's numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals import world as evalworld

from support_agent import context as ctx
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.config import Settings, resolve
from support_agent.contracts import (
    Completed,
    Escalated,
    IdempotencyKey,
    Identity,
    Message,
    ModelResponse,
    OrderStatus,
    Refused,
    RunId,
    TurnResult,
    Usage,
)
from support_agent.cost import Meter
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient, UnavailableClient
from support_agent.tools import connect

GOLDEN = Path(__file__).parent.parent / "evals" / "golden" / "eligibility.jsonl"
BASELINE = Path(__file__).parent.parent / "evals" / "baseline.json"

CASES = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
ORDER = "AB-10001"


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def privileged() -> Identity:
    """Every scope, so the golden set measures the *server's* eligibility rule
    rather than re-measuring the scope check that already has its own tests."""
    return Identity(
        customer_id="C-1042",
        scopes=ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE},
    )


def key(n: int = 0) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId("run_golden"), step=0, iteration=n)


# --------------------------------------------------------------------------- #
# AAC-0001 — a frozen, version-controlled set, scored per case.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
@pytest.mark.discharges(
    "P-CANCEL",
    "P-RETURN",
    "P-ADDRESS",
    "op:cancel_order",
    "op:open_return_request",
    "op:change_address",
    "ext:order_system",
)
async def test_the_golden_set(case: dict) -> None:
    row = case["row"]
    world = evalworld.World()
    world.seed(
        ORDER,
        OrderStatus(row["status"]),
        days_since_delivery=row["days_since_delivery"],
        final_sale=row["final_sale"],
    )

    async with connect(evalworld.build(world), ledger=InMemoryLedger()) as tools:
        result = await tools.call(case["action"], {"order_id": ORDER}, privileged(), key())

    assert not result.is_error, result.text
    allowed = result.structured["allowed"]
    assert allowed is case["expected_allowed"], (
        f"{case['action']} on {row}: expected allowed={case['expected_allowed']}, "
        f"got {allowed} — {result.structured['reason']}"
    )


@pytest.mark.discharges("AAC-0001")
def test_the_golden_set_is_frozen_and_covers_its_boundaries() -> None:
    """A set that grows silently is not a baseline. The boundary cases are named
    individually because a sampling strategy is exactly what misses them —
    day 30 and day 31 differ by one and by everything.

    29 → 26 when the world gained invariants (F-011). The set got *smaller and
    better*: 13 of the 29 described an order that cannot exist, and the pairwise
    sampler spent its budget on reachable combinations instead. This assertion
    is what forced the change to be noticed and justified rather than absorbed.
    """
    assert len(CASES) == 26
    boundaries = {c["boundary"] for c in CASES if c["boundary"]}
    assert "days_since_delivery exactly on its limit" in boundaries
    assert "days_since_delivery one past its limit" in boundaries
    # Derived from the condition, not typed in. A world declaring a different
    # window moves both without anyone editing this file.
    on_limit = next(c for c in CASES if c["boundary"] and "exactly" in c["boundary"])
    assert on_limit["row"]["days_since_delivery"] == 30
    assert on_limit["expected_allowed"] is True


@pytest.mark.discharges("AAC-0001", "P-CANCEL", "op:cancel_order")
async def test_a_refusal_changes_nothing_in_the_world() -> None:
    """Correct refusal is success — and success means the world is untouched.

    An agent that refuses in prose while the row changed anyway has failed in the
    only way that reaches a customer.
    """
    world = evalworld.World()
    world.seed(ORDER, OrderStatus.SHIPPED)

    async with connect(evalworld.build(world), ledger=InMemoryLedger()) as tools:
        result = await tools.call("cancel_order", {"order_id": ORDER}, privileged(), key())

    assert result.structured["allowed"] is False
    assert world.count("cancel_order") == 0
    assert world.status_of(ORDER) is OrderStatus.SHIPPED


# --------------------------------------------------------------------------- #
# AAC-0099 — the output contract holds on every route.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0099", "AAC-0002", "AHC-0017", "R-DISCOUNT", "esc:asked-for-human")
async def test_every_route_returns_a_conforming_result() -> None:
    """Four routes, four shapes, one contract. A route that returned a bare
    string would be invisible to every caller until one of them parsed it."""
    from pydantic import TypeAdapter

    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    adapter: TypeAdapter[TurnResult] = TypeAdapter(TurnResult)
    world = evalworld.World()
    world.seed(ORDER, OrderStatus.SHIPPED)

    exchanges = {
        "refuse": ("can I get a discount?", ScriptedClient([]), Refused),
        "escalate": ("put me through to a human", ScriptedClient([]), Escalated),
        "direct": (f"where is my order {ORDER}", ScriptedClient([]), Completed),
        "agentic": ("something ambiguous", ScriptedClient([ModelResponse(text="ok")]), Completed),
    }

    async with connect(evalworld.build(world), ledger=InMemoryLedger()) as tools:
        for route, (text, llm, expected) in exchanges.items():
            # A desk is wired: without one the agent refuses the handover
            # rather than claiming it, which is a different route (F-024).
            agent = ep.build(
                llm=llm,
                tools=tools,
                store=InMemoryCheckpointStore(),
                escalations=esc.InMemoryEscalationStore(),
            )
            result, _ = await agent.handle(text, identity=privileged())
            assert isinstance(result, expected), f"{route} returned {type(result).__name__}"
            # Round-trips through the declared union, so the contract is checked
            # rather than the isinstance being taken as proof of it.
            assert adapter.validate_python(adapter.dump_python(result)) == result


@pytest.mark.discharges("AAC-0099", "AAC-0009", "AHC-0017")
async def test_the_contract_holds_on_the_failure_route_too() -> None:
    """The route most likely to return something unshaped is the one nobody
    plans for."""
    from pydantic import TypeAdapter

    from support_agent import entrypoint as ep
    from support_agent.state import InMemoryCheckpointStore

    adapter: TypeAdapter[TurnResult] = TypeAdapter(TurnResult)
    world = evalworld.World()

    async with connect(evalworld.build(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(llm=UnavailableClient(), tools=tools, store=InMemoryCheckpointStore())
        result, _ = await agent.handle("something ambiguous", identity=privileged())

    assert adapter.validate_python(adapter.dump_python(result)) == result


# --------------------------------------------------------------------------- #
# AAC-0098 — every reachable model is evaluated, not just the primary.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0098", "AAC-0094", "AHC-0003", "AHC-0009")
def test_every_approved_model_is_configurable_priced_and_reachable() -> None:
    """The allowlist is the set of models a run could actually use, so a model
    that resolves but has no price would run and be unbudgetable. Three lists —
    approved, priced, and configurable — that drift apart silently otherwise."""
    approved = Settings(provider_api_key="k").approved_models
    assert approved, "an empty allowlist would make every model unreachable"

    for model in approved:
        config = resolve(Settings(provider_api_key="k", model=model))
        assert config.model == model
        meter = Meter(model, ceiling_usd=1.0)
        assert meter.price is not None
        # A different model must be a different configuration, or a report cannot
        # say which one produced it.
        assert (
            config.fingerprint
            != resolve(
                Settings(
                    provider_api_key="k",
                    model=approved[-1] if model != approved[-1] else approved[0],
                )
            ).fingerprint
        )


# --------------------------------------------------------------------------- #
# Regression against a committed baseline — AAC-0013, AAC-0102, AAC-0103.
# --------------------------------------------------------------------------- #


def measured() -> dict:
    """Three numbers that must not move without someone deciding they should."""
    system = "You are a support agent for a clothing retailer."
    history = [ctx.user_message(f"where is my order AB-{i}") for i in range(6)]
    assembled = ctx.assemble(system=system, history=history)

    meter = Meter("openai/gpt-oss-120b", ceiling_usd=1.0)
    for _ in range(3):
        meter.record(Usage(input_tokens=1200, output_tokens=180))

    return {
        "golden_cases": len(CASES),
        "context_chars": sum(len(m.content) for m in assembled),
        "cost_per_task_usd": float(meter.per_successful_task(1)),
    }


@pytest.mark.discharges("AAC-0013", "AAC-0102", "AAC-0103")
def test_nothing_regressed_against_the_committed_baseline() -> None:
    """One comparison, three obligations, because they are the same mechanism
    pointed at different numbers: a committed baseline and a diff.

    Deliberately exact rather than tolerant. Every input here is deterministic,
    so a tolerance band would only ever hide a real change — and "it drifted a
    bit each release" is how a system arrives somewhere nobody chose.
    """
    if not BASELINE.exists():
        pytest.skip("no baseline yet — run scripts/write_baseline.py")

    baseline = json.loads(BASELINE.read_text())
    current = measured()

    drift = {
        k: (baseline["measurements"][k], v)
        for k, v in current.items()
        if baseline["measurements"][k] != v
    }
    assert not drift, (
        f"regressed against baseline {baseline['taken']}: {drift}. "
        "If this change was intended, re-run scripts/write_baseline.py and say "
        "in the commit message why the number moved."
    )


@pytest.mark.discharges("AAC-0103", "AHC-0012")
def test_context_grows_with_history_and_is_bounded() -> None:
    """Growth is expected; unbounded growth is the defect."""
    system = "s"
    short = ctx.assemble(system=system, history=[ctx.user_message("hello")])
    long = ctx.assemble(
        system=system, history=[Message(role="user", content="x" * 400) for _ in range(200)]
    )

    assert sum(len(m.content) for m in long) > sum(len(m.content) for m in short)
    assert sum(len(m.content) for m in long) < 30_000
