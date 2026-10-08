"""The approval and escalation waits on DBOS (T-099), against a real PostgreSQL.

The same contract `tests/test_approvals.py` holds the Temporal workflow to —
expiry on the wait's clock, a stale grant asked again, a decline, a retried
far end, a decision our rule judges inside the wait — run on the adapter the
Azure stack binds. One table for approvals, one for escalations.

PostgreSQL is a throwaway cluster in pytest's temporary directory on a free
port, started once for the module and stopped in teardown, never left running.
With `AGENT_HARNESS_TEST_PG` set to a URL, that server is used instead. With
neither a URL nor `initdb` on the path, every row is skipped and says why.

Time is the wall's (DBOS has no time-skipping server), so the expiry rows wait
two or three real seconds.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("dbos", reason="the Azure extra (agent-harness[azure]) is not installed")

from agent_harness.approvals import dbos as approvals  # noqa: E402
from agent_harness.approvals.durable import FINAL, Ask, Assessment, CarriedOut  # noqa: E402
from agent_harness.approvals.workflow import ApprovalError, Terms  # noqa: E402
from agent_harness.contracts import (  # noqa: E402
    Approval,
    ApprovalState,
    EscalationState,
    IdempotencyKey,
    Identity,
    RunId,
)
from agent_harness.escalation import dbos as escalations  # noqa: E402
from agent_harness.escalation.workflow import EscalationError  # noqa: E402
from agent_harness.state import dbos as box  # noqa: E402

CUSTOMER = "C-1042"
BINARIES = [Path("/opt/homebrew/bin"), *Path("/opt/homebrew/opt").glob("postgresql*/bin")]


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in BINARIES:
        if (directory / name).exists():
            return str(directory / name)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run(command: list[str]) -> None:
    # A C locale, or the server refuses to start ("postmaster became
    # multithreaded during startup") wherever the shell's locale is unset.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    done = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if done.returncode != 0:
        pytest.fail(f"{Path(command[0]).name} failed: {done.stdout[-800:]}{done.stderr[-800:]}")


@pytest.fixture(scope="module")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server URL with no database in it; each row makes its own."""
    given = os.environ.get("AGENT_HARNESS_TEST_PG")
    if given:
        yield given.rstrip("/")
        return
    initdb, pg_ctl = _binary("initdb"), _binary("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("no PostgreSQL: set AGENT_HARNESS_TEST_PG or put initdb on the path")
    data = tmp_path_factory.mktemp("pg") / "data"
    _run([initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-locale"])
    port = _free_port()
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
    log = data.parent / "server.log"
    _run([pg_ctl, "-D", str(data), "-o", options, "-l", str(log), "-w", "start"])
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}"
    finally:
        _run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"])


@pytest.fixture
def database(postgres: str) -> Iterator[str]:
    """DBOS launched on a fresh database for one row, and shut down after it."""
    url = f"{postgres}/waits_{uuid.uuid4().hex[:10]}"
    box.launch(url, name="agent-harness-test")
    try:
        yield url
    finally:
        box.shutdown()


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


@dataclass
class Far:
    """The agent's steps, scripted: what the assessment says, and what each
    attempt to carry the action out does."""

    reason: str | None = "above the automatic limit"
    failed: str | None = None
    script: list[str] = field(default_factory=lambda: ["ok"])
    carried: int = 0
    assessed: int = 0
    reminded: int = 0

    async def assess(self, ask: Ask) -> Assessment:
        self.assessed += 1
        return Assessment(
            args={**ask.args, "amount": "12400"},
            reason=self.reason,
            approver="policy",
            failed=self.failed,
        )

    async def carry_out(self, approval: Approval) -> CarriedOut:
        move = self.script[min(self.carried, len(self.script) - 1)]
        self.carried += 1
        if move == "raise":
            raise ConnectionError("the far end dropped the connection")
        if move == "slow":
            await asyncio.sleep(5)
        if move == "stale":
            return CarriedOut(ok=False, text="total was '12400' and is now '13000'", stale=True)
        return CarriedOut(ok=True, text="refunded")

    async def remind(self, approval: Approval) -> None:
        self.reminded += 1


def _ask(far: Far, terms: Terms) -> Any:
    return approvals.DBOSApprovals(policy=terms, wait_s=20).request(
        action="issue_refund",
        args={"order_id": "AB-201"},
        identity=Identity(customer_id=CUSTOMER, scopes=frozenset({"orders:read"})),
        idempotency_key=IdempotencyKey(run_id=RunId("run_dbos"), step=2, iteration=1),
    )


DAY = Terms(ttl_s=86_400, remind_before_s=0)
SHORT = Terms(ttl_s=2, remind_before_s=0)
REMINDED = Terms(ttl_s=3, remind_before_s=2)

# (id, terms, far end, moves, final state, times carried out, reminders)
# A move is ("decide", granted, by, refusal it must meet or None), ("wait", s),
# ("restart",) or ("unknown",): a decision on an approval that does not exist.
APPROVALS = [
    ("within the limit: carried out at once", DAY, Far(reason=None), [], "done", 1, 0),
    ("granted by a colleague", DAY, Far(), [("decide", True, "ops-7", None)], "done", 1, 0),
    ("declined", DAY, Far(), [("decide", False, "ops-7", None)], "refused", 0, 0),
    (
        "the customer cannot grant their own",
        DAY,
        Far(),
        [("decide", True, CUSTOMER, "customer it belongs to"), ("decide", True, "ops-7", None)],
        "done",
        1,
        0,
    ),
    (
        "granted twice is one effect",
        DAY,
        Far(),
        [("decide", True, "ops-7", None), ("decide", True, "ops-8", "already decided")],
        "done",
        1,
        0,
    ),
    (
        "nobody came: expired, and too late to grant",
        SHORT,
        Far(),
        [("wait", 3), ("decide", True, "ops-7", "has expired")],
        "expired",
        0,
        0,
    ),
    ("reminded before it expires", REMINDED, Far(), [("wait", 4)], "expired", 0, 1),
    (
        "a flaky far end is retried",
        DAY,
        Far(script=["raise", "raise", "ok"]),
        [("decide", True, "ops-7", None)],
        "done",
        3,
        0,
    ),
    (
        "a timeout, then an answer",
        DAY,
        Far(script=["slow", "ok"]),
        [("decide", True, "ops-7", None)],
        "done",
        2,
        0,
    ),
    (
        "a far end that never answers in time",
        DAY,
        Far(script=["slow"]),
        [("decide", True, "ops-7", None)],
        "failed",
        3,
        0,
    ),
    (
        "a stale grant is asked again",
        DAY,
        Far(script=["stale"]),
        [("decide", True, "ops-7", None)],
        "stale",
        1,
        0,
    ),
    ("the assessment refused it", DAY, Far(failed="not owed"), [], "failed", 0, 0),
    (
        "no such approval",
        DAY,
        Far(),
        [("unknown",), ("decide", False, "ops-7", None)],
        "refused",
        0,
        0,
    ),
    (
        "a restart while it waits resumes it",
        DAY,
        Far(),
        [("restart",), ("decide", True, "ops-7", None)],
        "done",
        1,
        0,
    ),
]


@pytest.mark.parametrize(
    ("case", "terms", "far", "moves", "final", "carried", "reminded"),
    APPROVALS,
    ids=[a[0] for a in APPROVALS],
)
@pytest.mark.discharges("AHC-0057")
def test_an_approval_waits_on_dbos(
    database: str,
    case: str,
    terms: Terms,
    far: Far,
    moves: list[tuple[Any, ...]],
    final: str,
    carried: int,
    reminded: int,
) -> None:
    approvals.serve(
        approvals.Work(
            assess=far.assess,
            carry_out=far.carry_out,
            remind=far.remind,
            retry_interval_s=0.05,
            timeout_s=0.5,
        )
    )
    reader = approvals.DBOSApprovals(wait_s=20)
    desk = approvals.DBOSApprovalDesk(wait_s=20)

    async def scenario() -> Approval:
        asked = await _ask(far, terms)
        for move in moves:
            if move[0] == "wait":
                await asyncio.sleep(move[1])
            elif move[0] == "restart":
                box.shutdown()
                box.launch(database, name="agent-harness-test")
            elif move[0] == "unknown":
                with pytest.raises(ApprovalError, match="no approval"):
                    await desk.decide("apr_nope", granted=True, by="ops-7")
            else:
                _, granted, by, refused = move
                if refused is None:
                    await desk.decide(asked.id, granted=granted, by=by)
                    continue
                with pytest.raises(ApprovalError, match=refused):
                    await desk.decide(asked.id, granted=granted, by=by)
        await _settled(reader, asked.id)
        found = await reader.get(asked.id)
        assert found is not None
        if final == "stale":
            fresh = await reader.get(f"{asked.id}~2")
            assert found.superseded_by == f"{asked.id}~2", case
            assert fresh is not None and fresh.supersedes == asked.id, case
        return found

    found = asyncio.run(scenario())
    assert found.state.value == final, case
    assert far.carried == carried, case
    assert far.reminded == reminded, case
    if final == "failed" and carried:
        assert (found.result or "").startswith("not carried out"), case


async def _settled(reader: approvals.DBOSApprovals, approval_id: str) -> None:
    for _ in range(100):
        found = await reader.get(approval_id)
        if found is not None and (
            found.state in FINAL or (found.state is ApprovalState.WAITING and not found.decided)
        ):
            return
        await asyncio.sleep(0.1)


def test_the_same_request_is_one_wait_and_the_queue_shows_only_what_waits(database: str) -> None:
    waiting, automatic = Far(), Far(reason=None)
    approvals.serve(approvals.Work(assess=waiting.assess, carry_out=waiting.carry_out))

    async def scenario() -> None:
        first = await _ask(waiting, DAY)
        again = await _ask(waiting, DAY)
        assert first.id == again.id and waiting.assessed == 1
        approvals.serve(approvals.Work(assess=automatic.assess, carry_out=automatic.carry_out))
        done = await approvals.DBOSApprovals(policy=DAY).request(
            action="issue_refund",
            args={"order_id": "AB-202"},
            identity=Identity(customer_id=CUSTOMER, scopes=frozenset()),
            idempotency_key=IdempotencyKey(run_id=RunId("run_dbos"), step=3, iteration=1),
        )
        assert done.state is ApprovalState.DONE
        listed = await approvals.DBOSApprovals().pending()
        assert [a.id for a in listed] == [first.id]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Escalations
# --------------------------------------------------------------------------- #

# (id, ttl, moves, final state, still held on the conversation)
# A move is ("resolve", outcome, by, refusal or None) or ("wait", s).
ESCALATIONS = [
    ("resolved by a colleague", 600, [("resolve", "resolved", "ops-7", None)], "resolved", False),
    (
        "an outcome outside the declared set",
        600,
        [("resolve", "sorted it", "ops-7", "is not an outcome")],
        "queued",
        True,
    ),
    (
        "the customer cannot close their own",
        600,
        [("resolve", "resolved", CUSTOMER, "customer it belongs to")],
        "queued",
        True,
    ),
    (
        "closing is terminal",
        600,
        [("resolve", "resolved", "ops-7", None), ("resolve", "resolved", "ops-8", "already")],
        "resolved",
        False,
    ),
    (
        "nobody came: it lapses, and cannot be resolved after",
        2,
        [("wait", 3), ("resolve", "resolved", "ops-7", "lapsed before anyone came")],
        "expired",
        False,
    ),
]


@pytest.mark.parametrize(
    ("case", "ttl", "moves", "final", "held"), ESCALATIONS, ids=[e[0] for e in ESCALATIONS]
)
@pytest.mark.discharges("P-ESC-OUTCOME", "P-ESC-TTL")
def test_an_escalation_waits_on_dbos(
    database: str, case: str, ttl: int, moves: list[tuple[Any, ...]], final: str, held: bool
) -> None:
    raising = escalations.DBOSEscalations()
    desk = escalations.DBOSEscalationDesk(wait_s=20)

    async def scenario() -> tuple[EscalationState, bool]:
        raised = await raising.raise_for(
            conversation_id="conv-1",
            run_id="run_dbos",
            customer_id=CUSTOMER,
            reason="asked for a person",
            rule_id="R-HUMAN",
            rules_version="1",
            ttl_s=ttl,
        )
        assert raised.state is EscalationState.QUEUED
        for move in moves:
            if move[0] == "wait":
                await asyncio.sleep(move[1])
                continue
            _, outcome, by, refused = move
            if refused is None:
                await desk.resolve(raised.id, outcome=outcome, by=by)
                continue
            with pytest.raises(EscalationError, match=refused):
                await desk.resolve(raised.id, outcome=outcome, by=by)
        found = await raising.get(raised.id)
        assert found is not None
        return found.state, await raising.open_for("conv-1") is not None

    state, still_held = asyncio.run(scenario())
    assert state.value == final, case
    assert still_held is held, case
