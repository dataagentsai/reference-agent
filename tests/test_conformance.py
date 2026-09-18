"""The report, and two gaps it exposed.

A conformance report is only worth reading if it can say what it did *not*
cover, so most of this file is about that half.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import router
from support_agent import telemetry as tel
from support_agent.conformance import Obligation, Report, Verdict, load, load_elsewhere
from support_agent.contracts import Identity, SideEffectClass, ToolResult, ToolSpec
from support_agent.idempotency import InMemoryLedger
from support_agent.tools import META_SIDE_EFFECT, connect


def obligation(oid: str = "AAC-0055", *, gate: bool = False) -> Obligation:
    return Obligation(
        id=oid,
        title="t",
        dimension="reliability",
        level="MUST",
        gate=gate,
        stages=("S2",),
        mechanisms=("M1",),
    )


# --------------------------------------------------------------------------- #
# The manifest cites, it does not restate.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_the_manifest_carries_ids_but_not_normative_text() -> None:
    """Principle 1. The obligation's `statement` stays in the catalog; copying
    it here would fork the normative text into a second place that drifts."""
    import json

    from support_agent.conformance import MANIFEST

    raw = json.loads(MANIFEST.read_text())
    for entry in raw["obligations"]:
        assert "statement" not in entry
        assert set(entry) == {"id", "title", "dimension", "level", "gate", "stages", "mechanisms"}


@pytest.mark.tooling
def test_the_manifest_holds_every_a6_obligation() -> None:
    """43 → 49 when the manifest was synced from the catalog for the first time.

    It had been hand-maintained and had drifted by six, which is why
    `scripts/sync_obligations.py` now exists. The count stays pinned so the next
    drift is a failing test rather than a quietly shrinking denominator.

    49 → 50 on 2026-09-12: AAC-0111, written because this agent's authentication
    was tested and required by nothing (G0.6).

    50 → 51 on 2026-09-12: AAC-0112, written because a reader watching a run
    asked why the agent promised to check something and never came back (F-035).

    51 → 52 on 2026-09-13: AAC-0113, the gap the reference's own design doc
    listed at 16 and nothing had built — acting on a read the world has moved
    on from (G0.6).

    52 → 55 on 2026-09-19: AAC-0114, AAC-0115 and AAC-0116, written when asking
    whether an operator would learn of a broken agent before a customer did
    found that nothing in the catalog required it (T-055, T-056, T-057).
    """
    assert len(load()) == 55


# --------------------------------------------------------------------------- #
# What the report says.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_an_untouched_obligation_is_not_exercised_not_passing() -> None:
    """A report listing only passes and failures implies the remainder is fine."""
    report = Report([obligation()])
    assert report.by_verdict(Verdict.NOT_EXERCISED)[0].obligation.id == "AAC-0055"
    assert report.exercised == 0


@pytest.mark.tooling
def test_one_failure_outweighs_any_number_of_passes() -> None:
    report = Report([obligation()])
    report.record("AAC-0055", "t::a", passed=True)
    report.record("AAC-0055", "t::b", passed=False)
    report.record("AAC-0055", "t::c", passed=True)
    assert report.coverage["AAC-0055"].verdict is Verdict.FAILED


@pytest.mark.tooling
def test_scored_and_binary_are_never_summed_together() -> None:
    """A fix moving a model-driven case from 60% to 80% is real progress and
    invisible to pass/fail. Mixing the columns is how a team chases flakes."""
    report = Report([obligation("AAC-0003"), obligation("AAC-0055")])
    report.record("AAC-0003", "t::judge", passed=True, scored=True)
    report.record("AAC-0055", "t::budget", passed=True)
    assert len(report.by_verdict(Verdict.SCORED)) == 1
    assert len(report.by_verdict(Verdict.PASSED)) == 1


@pytest.mark.tooling
def test_an_uncovered_release_gate_is_called_out_separately() -> None:
    """An uncovered gate is a claim the release process makes and the suite
    cannot support."""
    report = Report([obligation("AAC-0001", gate=True), obligation("AAC-0007")])
    assert [c.obligation.id for c in report.gates_uncovered] == ["AAC-0001"]


@pytest.mark.tooling
def test_an_obligation_from_another_archetype_is_a_tagging_gap_not_a_mistake() -> None:
    """The mechanism, on a fixture rather than a live id.

    It used to assert on AAC-0047 directly. That broke — for the best possible
    reason, see below — which is itself the lesson: a test of a *mechanism*
    should not be anchored to a real identifier that the catalog is expected to
    move. The next line down is where the live fact belongs.
    """
    report = Report([obligation()])
    report.record("AAC-0017", "tests/test_x.py::somewhere", passed=True)
    assert "AAC-0017" in report.tagging_gaps
    assert report.unknown_ids == set()


@pytest.mark.tooling
def test_g1_was_accepted_by_the_catalog() -> None:
    """**G1, closed — and we did not know.**

    The conformance report spent a day reporting AAC-0029, AAC-0046 and AAC-0047
    as *exercised but not tagged A6*: passing A6 tests were discharging
    obligations the catalog listed for other archetypes only. That was the case
    for a catalog change, generated mechanically rather than argued.

    **The catalog accepted it.** All three are A6 as of 0.12.0. The agent's
    manifest was hand-maintained and never picked it up, so the evidence went on
    being re-reported as an open gap long after it had been acted on.

    Pinned as a fact because it is the one that closes the loop: a finding
    produced by the runtime changed the specification.
    """
    a6 = {o.id for o in load()}
    assert {"AAC-0029", "AAC-0046", "AAC-0047"} <= a6
    assert not {"AAC-0029", "AAC-0046", "AAC-0047"} & set(load_elsewhere())


@pytest.mark.tooling
def test_an_id_that_exists_nowhere_is_a_defect_in_the_test() -> None:
    report = Report([obligation()])
    report.record("AAC-9999", "t::typo", passed=True)
    assert report.unknown_ids == {"AAC-9999"}


@pytest.mark.tooling
def test_the_rendered_report_names_what_it_did_not_cover() -> None:
    report = Report([obligation("AAC-0001", gate=True), obligation("AAC-0055")])
    report.record("AAC-0055", "t::a", passed=True)
    rendered = report.render()
    assert "NOT exercised 1" in rendered
    assert "AAC-0001" in rendered


# --------------------------------------------------------------------------- #
# Two gaps the report exposed, where the capability existed and no test did.
# --------------------------------------------------------------------------- #


class BigOut(BaseModel):
    order_id: str
    blob: str


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


@pytest.fixture
def server():
    srv = MCPServer("ecom")

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_history(order_id: str) -> BigOut:
        """Return far more than should ever enter context."""
        return BigOut(order_id=order_id, blob="x" * 50_000)

    return srv


@pytest.mark.discharges("AAC-0105", "AHC-0038")
async def test_a_large_tool_result_is_bounded_before_it_enters_context(server) -> None:
    """Over MCP, end to end: what the model is sent, not what the client holds."""
    from support_agent import context as ctx
    from support_agent.contracts import IdempotencyKey, RunId

    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
    key = IdempotencyKey(run_id=RunId("run_b"), step=0, iteration=0)

    async with connect(server, ledger=InMemoryLedger(), max_result_chars=1000) as tools:
        result = await tools.call("get_history", {"order_id": "AB-1"}, who, key)

    assert result.truncated
    assert "x" * 1001 not in ctx.tool_message(result, tool_call_id="tc").content


LIMIT = 1000
BIG = "x" * 50_000

# (name, what the server answered, whether it is cut)
RESULTS = [
    ("a large structured result with a short text block — F-023's shape",
     ToolResult(name="t", structured={"blob": BIG}, text="see structured"), True),
    ("a large structured result with its text duplicate",
     ToolResult(name="t", structured={"blob": BIG}, text=BIG), True),
    ("a large text-only result", ToolResult(name="t", text=BIG), True),
    ("a small structured result passes whole",
     ToolResult(name="t", structured={"id": "AB-1"}), False),
    ("a small text result passes whole", ToolResult(name="t", text="ok"), False),
]  # fmt: skip


class Answers:
    """A transport that answers every call with one result — the bound, alone."""

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    async def advertised(self) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
        spec = ToolSpec(
            name="t",
            description="a tool",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=SideEffectClass.READ,
        )
        return (spec,), ()

    async def invoke(self, name, arguments, *, caller, idempotency_key=None) -> ToolResult:
        return self.result


@pytest.mark.discharges("AAC-0105", "AHC-0038", "Q-TOOL-RESULT")
@pytest.mark.parametrize(("name", "answered", "cut"), RESULTS, ids=[r[0] for r in RESULTS])
async def test_what_enters_context_is_bounded_whatever_its_shape(
    name: str, answered: ToolResult, cut: bool
) -> None:
    """Q-TOOL-RESULT is about what *enters context*. The first test asserted on
    the text block, which the context never sends when structured content is
    present — so it passed while 50,000 characters reached the model (F-023)."""
    from support_agent import context as ctx
    from support_agent.contracts import IdempotencyKey, RunId
    from support_agent.tools import GatedTools

    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
    key = IdempotencyKey(run_id=RunId("run_b"), step=0, iteration=0)
    tools = GatedTools(Answers(answered), ledger=InMemoryLedger(), max_result_chars=LIMIT)

    result = await tools.call("t", {}, who, key)
    sent = result.for_context()

    assert len(sent) <= LIMIT
    assert sent in ctx.tool_message(result, tool_call_id="tc").content
    assert result.truncated is cut
    assert ("[truncated from" in sent) is cut, "a cut result says so"
    assert result.structured == answered.structured, "the checks still read it whole"


DEGENERATE = [
    ("empty", ""),
    ("whitespace only", "   \n\t  "),
    ("very long", "help " * 20_000),
    ("control characters", "cancel\x00\x01\x02 order"),
    ("emoji flood", "😀" * 5_000),
    ("nested delimiters", "<<<untrusted>>>" * 100),
]


@pytest.mark.parametrize(("name", "text"), DEGENERATE, ids=[c[0] for c in DEGENERATE])
@pytest.mark.discharges("AAC-0015")
def test_the_router_has_defined_behaviour_on_degenerate_input(name: str, text: str) -> None:
    """Defined, not merely non-crashing: every one of these must yield a typed
    Route, and none may resolve to a Direct action on garbage."""
    decision = router.route(text)
    assert decision.kind in {"direct", "agentic", "refuse", "escalate"}
    assert decision.kind != "direct"
