"""Contracts carry design decisions. These tests pin the decisions, not the syntax."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from support_agent.contracts import (
    Agentic,
    Completed,
    Direct,
    Escalate,
    Escalated,
    Failed,
    IdempotencyKey,
    Identity,
    Intent,
    NeedsApproval,
    Refuse,
    Refused,
    Route,
    RunId,
    SideEffectClass,
    TerminationReason,
    ToolRegistry,
    ToolSpec,
    TurnResult,
)

RUN = RunId("run_abc")


def key(step: int, iteration: int) -> IdempotencyKey:
    return IdempotencyKey(run_id=RUN, step=step, iteration=iteration)


# --------------------------------------------------------------------------- #
# AHC-0074: a retry keeps run, step and iteration; a legitimate second
# execution of the same step changes the iteration. An attempt counter in the
# key would defeat deduplication entirely.
# --------------------------------------------------------------------------- #

IDEMPOTENCY_CASES = [
    ("retry of the same step is the same key", key(1, 0), key(1, 0), True),
    ("second execution in a loop is a new key", key(1, 0), key(1, 1), False),
    ("a different step is a different key", key(1, 0), key(2, 0), False),
]


@pytest.mark.parametrize(
    ("name", "left", "right", "same"),
    IDEMPOTENCY_CASES,
    ids=[c[0] for c in IDEMPOTENCY_CASES],
)
@pytest.mark.discharges("AAC-0047", "AHC-0074")
def test_idempotency_key_identity(
    name: str, left: IdempotencyKey, right: IdempotencyKey, *, same: bool
) -> None:
    assert (left.value == right.value) is same


@pytest.mark.discharges("AHC-0074")
def test_idempotency_key_is_stable_across_construction() -> None:
    assert key(3, 2).value == "run_abc:3:2"


# --------------------------------------------------------------------------- #
# Side-effect class decides whether a key is required at the tool boundary.
# A read-only agent leaves IRREVERSIBLE empty — which is why a support agent,
# not a cost analyst, is the reference.
# --------------------------------------------------------------------------- #

SIDE_EFFECT_CASES = [
    (SideEffectClass.READ, False),
    (SideEffectClass.REVERSIBLE, True),
    (SideEffectClass.IRREVERSIBLE, True),
]


@pytest.mark.parametrize(("cls", "needs_key"), SIDE_EFFECT_CASES, ids=lambda v: str(v))
@pytest.mark.discharges("AHC-0074")
def test_side_effect_class_decides_idempotency(cls: SideEffectClass, needs_key: bool) -> None:
    assert cls.requires_idempotency_key is needs_key


# --------------------------------------------------------------------------- #
# Route: four outcomes, and two of them never cost a loop iteration.
# --------------------------------------------------------------------------- #

ROUTE_ADAPTER: TypeAdapter[Route] = TypeAdapter(Route)

ROUTE_CASES = [
    ("direct", Direct(intent=Intent.ORDER_STATUS, handler="lookup")),
    ("agentic", Agentic(goal="cancel one of two orders")),
    ("refuse", Refuse(reason="discount negotiation is out of scope")),
    ("escalate", Escalate(reason="customer asked for a human")),
]


@pytest.mark.parametrize(("kind", "route"), ROUTE_CASES, ids=[c[0] for c in ROUTE_CASES])
def test_route_round_trips_through_its_discriminator(kind: str, route: Route) -> None:
    restored = ROUTE_ADAPTER.validate_python(ROUTE_ADAPTER.dump_python(route))
    assert restored == route
    assert restored.kind == kind


@pytest.mark.parametrize(
    ("route", "reaches_model"),
    [
        (Direct(intent=Intent.ORDER_STATUS, handler="lookup"), False),
        (Refuse(reason="out of scope"), False),
        (Escalate(reason="asked for a human"), False),
        (Agentic(goal="ambiguous"), True),
    ],
    ids=["direct", "refuse", "escalate", "agentic"],
)
def test_only_the_agentic_route_reaches_the_model(route: Route, reaches_model: bool) -> None:
    assert (route.kind == "agentic") is reaches_model


# --------------------------------------------------------------------------- #
# TurnResult: every exit is typed and assertable without parsing prose, because
# an evaluation drives this surface (AHC-0010).
# --------------------------------------------------------------------------- #

TURN_ADAPTER: TypeAdapter[TurnResult] = TypeAdapter(TurnResult)

TURN_CASES = [
    ("completed", Completed(reply="Your order ships tomorrow.")),
    (
        "needs_approval",
        NeedsApproval(
            approval_id="apr_1",
            action="issue_refund",
            reason="above the approval threshold",
            reply="I have sent this for approval.",
        ),
    ),
    ("refused", Refused(reply="I cannot do that.", reason="out of scope")),
    ("escalated", Escalated(reply="Passing you to a colleague.", reason="asked for a human")),
    ("failed", Failed(customer_message="Something went wrong.", detail="tool server 503")),
]


@pytest.mark.parametrize(("kind", "result"), TURN_CASES, ids=[c[0] for c in TURN_CASES])
@pytest.mark.discharges("AAC-0002", "AHC-0017")
def test_turn_result_round_trips(kind: str, result: TurnResult) -> None:
    restored = TURN_ADAPTER.validate_python(TURN_ADAPTER.dump_python(result))
    assert restored == result
    assert restored.kind == kind


def test_needs_approval_does_not_block_and_says_why() -> None:
    """L14's question is what the system does while it waits: it returns."""
    r = NeedsApproval(approval_id="a", action="issue_refund", reason="over threshold", reply="ok")
    assert r.termination is TerminationReason.AWAITING_APPROVAL


def test_failure_separates_customer_message_from_operator_detail() -> None:
    f = Failed(customer_message="Something went wrong.", detail="psycopg: connection refused")
    assert "psycopg" not in f.customer_message


# --------------------------------------------------------------------------- #
# Identity: the token must not reach a log by accident.
# --------------------------------------------------------------------------- #


def test_identity_token_is_not_in_its_repr() -> None:
    ident = Identity(customer_id="C-1", scopes=frozenset({"orders:read"}), token="secret-value")
    assert "secret-value" not in repr(ident)
    assert ident.may("orders:read")
    assert not ident.may("refunds:write")


# --------------------------------------------------------------------------- #
# Registry: the irreversible surface is what approval gates and idempotency
# keys are dimensioned against.
# --------------------------------------------------------------------------- #


def _spec(name: str, cls: SideEffectClass) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=cls,
    )


@pytest.mark.discharges("AHC-0039")
def test_registry_isolates_the_irreversible_surface() -> None:
    registry = ToolRegistry(
        tools=(
            _spec("get_order", SideEffectClass.READ),
            _spec("open_return_request", SideEffectClass.REVERSIBLE),
            _spec("issue_refund", SideEffectClass.IRREVERSIBLE),
            _spec("cancel_order", SideEffectClass.IRREVERSIBLE),
        )
    )
    assert {t.name for t in registry.irreversible} == {"issue_refund", "cancel_order"}
    assert registry.get("get_order") is not None
    assert registry.get("nonexistent") is None
