"""A3 — our own approval and escalation records: one table of rows, every adapter.

The `records` port's adapters (`in-memory`, `postgres`) are built through the
registry from an overlay's settings, as a deployment builds them, and each runs
the same rows: a record written is read back; its status moves; written twice
(a step re-run after a crash) it is one row; an unknown id is nothing. Then the
far end's check (`contracts.records.refusals`) on a record read back from each
store: the same call is covered, and another amount, claim, person or key, an
expired grant or one not yet granted is refused, naming why.

PostgreSQL is the server `test_dbos_waits` provides (a throwaway cluster, or
`AGENT_HARNESS_TEST_PG`), a fresh database for each row with `sql/002_records.sql`
applied, dropped after it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_dbos_waits

from agent_harness.adapters import ADAPTERS, Wiring
from agent_harness.contracts import Approval, ApprovalState, Escalation, EscalationState
from agent_harness.contracts.records import (
    ApprovalRecord,
    RecordStore,
    approval_record,
    escalation_record,
    refusals,
)

postgres = test_dbos_waits.postgres
"""The server `test_dbos_waits` starts or is given; each row makes its own database."""

DDL = Path(__file__).resolve().parents[1] / "sql" / "002_records.sql"
NOW = 1_800_000_000
KEY = "run_1:3:0"
WHO = "C-1042"
ADAPTER_NAMES = ["in-memory", "postgres"]


@asynccontextmanager
async def store(name: str, request: pytest.FixtureRequest) -> AsyncIterator[RecordStore]:
    """The adapter `name`, built through the registry, on a fresh database if it needs one."""
    chosen = ADAPTERS.load("records", name)
    settings: dict[str, Any] = {k: s.default for k, s in chosen.settings.items()}
    if "url" not in settings:
        async with chosen.build(Wiring(settings, {}, {}, "test")) as built:
            yield built
        return
    server = request.getfixturevalue("postgres")
    database = f"records_{uuid.uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(f"{server}/postgres", autocommit=True) as c:
        await c.execute(f'CREATE DATABASE "{database}"')
    url = f"{server}/{database}"
    try:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
            await c.execute(DDL.read_text().encode())
        async with chosen.build(Wiring({**settings, "url": url}, {}, {}, "test")) as built:
            yield built
    finally:
        async with await psycopg.AsyncConnection.connect(
            f"{server}/postgres", autocommit=True
        ) as c:
            await c.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def approval(state: ApprovalState = ApprovalState.CARRYING_OUT, **changed: Any) -> Approval:
    base: dict[str, Any] = {
        "id": "apr_1",
        "action": "issue_payout",
        "args": {"claim_id": "CLM-010004", "amount": 25001},
        "reason": "above the automatic limit",
        "customer_id": WHO,
        "conversation_id": "conv-1",
        "idempotency_key": KEY,
        "created_at": NOW - 60,
        "expires_at": NOW + 3600,
        "decided": state is not ApprovalState.WAITING,
        "granted": state is not ApprovalState.WAITING,
        "decided_by": None if state is ApprovalState.WAITING else "ops-7",
        "state": state,
    }
    return Approval(**{**base, **changed})


def without_stamp(record: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = record.model_dump(exclude={"updated_at"})
    return dumped


# (case, states written in order, id read, the status read back or None)
STORE_ROWS = [
    ("written, read back", [ApprovalState.WAITING], "apr_1", "waiting"),
    (
        "the status moves with the wait",
        [ApprovalState.ASSESSING, ApprovalState.WAITING, ApprovalState.CARRYING_OUT],
        "apr_1",
        "carrying_out",
    ),
    (
        "written twice, a step re-run after a crash, is one row",
        [ApprovalState.DONE, ApprovalState.DONE],
        "apr_1",
        "done",
    ),
    ("an unknown id is nothing", [ApprovalState.WAITING], "apr_unknown", None),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize(
    ("case", "states", "read", "status"), STORE_ROWS, ids=[r[0] for r in STORE_ROWS]
)
async def test_an_approval_record_is_kept_as_the_wait_wrote_it(
    name: str,
    case: str,
    states: list[ApprovalState],
    read: str,
    status: str | None,
    request: pytest.FixtureRequest,
) -> None:
    async with store(name, request) as records:
        for state in states:
            await records.put_approval(approval_record(approval(state)))
        found = await records.approval(read)
    if status is None:
        assert found is None, case
        return
    assert found is not None, case
    assert found.status == status, case
    assert without_stamp(found) == without_stamp(approval_record(approval(states[-1]))), case
    assert found.updated_at > 0, "the store stamps every write"


# (case, the state written, what differs in the call, when it is checked, said)
CHECK_ROWS: list[tuple[str, ApprovalState, dict[str, Any], int, list[str]]] = [
    ("the same call is covered", ApprovalState.CARRYING_OUT, {}, NOW, []),
    ("carried out already, still covered", ApprovalState.DONE, {}, NOW, []),
    (
        "another amount",
        ApprovalState.CARRYING_OUT,
        {"args": {"claim_id": "CLM-010004", "amount": "25002"}},
        NOW,
        ["another amount", "args_digest"],
    ),
    (
        "another claim",
        ApprovalState.CARRYING_OUT,
        {"args": {"claim_id": "CLM-010001", "amount": "25001"}},
        NOW,
        ["another claim_id", "args_digest"],
    ),
    (
        "another person",
        ApprovalState.CARRYING_OUT,
        {"requested_for": "C-2001"},
        NOW,
        ["another person", "args_digest"],
    ),
    (
        "another key",
        ApprovalState.CARRYING_OUT,
        {"idempotency_key": "run_2:1:0"},
        NOW,
        ["another call", "args_digest"],
    ),
    ("expired", ApprovalState.CARRYING_OUT, {}, NOW + 3600, ["expired"]),
    ("not yet granted", ApprovalState.WAITING, {}, NOW, ["not granted (it is waiting)"]),
    ("refused by a person", ApprovalState.REFUSED, {}, NOW, ["not granted (it is refused)"]),
]


@pytest.mark.discharges("AHC-0057", "AHC-0022")
@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize(
    ("case", "state", "call", "now", "said"), CHECK_ROWS, ids=[r[0] for r in CHECK_ROWS]
)
async def test_a_record_covers_only_the_call_it_was_decided_on(
    name: str,
    case: str,
    state: ApprovalState,
    call: dict[str, Any],
    now: int,
    said: list[str],
    request: pytest.FixtureRequest,
) -> None:
    async with store(name, request) as records:
        await records.put_approval(approval_record(approval(state)))
        found = await records.approval("apr_1")
    assert isinstance(found, ApprovalRecord)
    checked = {
        "action": "issue_payout",
        "args": {"claim_id": "CLM-010004", "amount": 25001},
        "requested_for": WHO,
        "idempotency_key": KEY,
        **call,
    }
    why = refusals(found, now=now, **checked)
    if not said:
        assert why == [], case
    for words in said:
        assert any(words in reason for reason in why), f"{case}: {why}"


def escalation(state: EscalationState, **changed: Any) -> Escalation:
    base: dict[str, Any] = {
        "id": "esc_1",
        "conversation_id": "conv-1",
        "run_id": "run_1",
        "customer_id": WHO,
        "context": "claim CLM-010004; the policyholder asked for a person",
        "rule_id": "R-ASKED",
        "rules_version": "1",
        "reason": "the policyholder asked for a person",
        "state": state,
        "created_at": NOW,
        "expires_at": NOW + 3600,
        "outcome_by": None if state is EscalationState.QUEUED else "asha",
    }
    return Escalation(**{**base, **changed})


# (case, states written in order, id read, status read back or None)
ESCALATION_ROWS = [
    ("written, read back", [EscalationState.QUEUED], "esc_1", "queued"),
    ("resolved", [EscalationState.QUEUED, EscalationState.RESOLVED], "esc_1", "resolved"),
    ("lapsed", [EscalationState.QUEUED, EscalationState.EXPIRED], "esc_1", "expired"),
    ("an unknown id is nothing", [EscalationState.QUEUED], "esc_unknown", None),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize("name", ADAPTER_NAMES)
@pytest.mark.parametrize(
    ("case", "states", "read", "status"), ESCALATION_ROWS, ids=[r[0] for r in ESCALATION_ROWS]
)
async def test_an_escalation_record_is_kept_as_the_wait_wrote_it(
    name: str,
    case: str,
    states: list[EscalationState],
    read: str,
    status: str | None,
    request: pytest.FixtureRequest,
) -> None:
    async with store(name, request) as records:
        for state in states:
            await records.put_escalation(escalation_record(escalation(state)))
        found = await records.escalation(read)
    if status is None:
        assert found is None, case
        return
    assert found is not None and found.status == status, case
    expected = escalation_record(escalation(states[-1]))
    assert without_stamp(found) == without_stamp(expected), case
