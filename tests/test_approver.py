"""The approver as an actor — F-007.

Every existing approval test decides by calling `decide(...)` inline, instantly,
with `granted=True`. That exercises the queue. It cannot express a reviewer who
takes an hour, says no, walks away, or answers after the window has closed — and
those are the four things a real one does.

The scenarios below run the whole path: a customer asks for a large refund, the
agent requests approval and stops, a human decides (or does not) between turns,
and the customer comes back to find out what happened.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from agenttwin import Approver, Clock, Live, Scenario, ScriptedActor, load, project, run_scenario
from evals import durable

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
ORDER = "AB-10003"
BIG = "24000"  # over the ₹10,000 threshold, so a human is required
HOUR = 3600
DAY = 24 * HOUR


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def asks_for_refund() -> ScriptedClient:
    """Turn one requests the refund; turn two is the customer following up."""
    return ScriptedClient(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name=ap.REQUEST_REFUND,
                        arguments={"order_id": ORDER, "amount": BIG},
                    ),
                )
            ),
            ModelResponse(text="Let me check on that for you."),
            ModelResponse(text="Let me check on that for you."),
        ]
    )


@asynccontextmanager
async def conversation(decision: str, *, by: str = "ops-7", turns: int = 3, step_s: int = HOUR):
    """A customer who asks, then follows up, while a reviewer does or does not act.

    The approval is a Temporal workflow on the test server, on the scenario's
    clock (T-028), so *takes an hour*, *walks away* and *answers too late* are
    the workflow's own timer rather than a number in a store.
    """
    world = Live.start(load(WORLD))
    clock = Clock(step_s=step_s)
    scenario = Scenario(name="a refund that needs a human", max_turns=turns, predicates={})
    actor = ScriptedActor([f"please refund my order {ORDER}", "any update?", "any update?"][:turns])

    async with (
        connect(project(world), requests=InMemoryRequests()) as tools,
        durable.approvals_for(tools, clock=clock) as waits,
    ):
        approver = DECISIONS[decision](
            waits.approvals, durable.decide(waits.desk), name=by, delay_s=DELAYS[decision]
        )
        agent = ep.build(
            llm=asks_for_refund(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=waits.approvals,
            clock=clock,
        )
        record = await run_scenario(
            scenario,
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            approver=approver,
            clock=clock,
        )
        yield world, record, approver


DECISIONS = {
    "grants": Approver.grants,
    "denies": Approver.denies,
    "silent": Approver.silent,
    "too late": Approver.grants,
    "slow": Approver.grants,
}
DELAYS = {"grants": 0, "denies": 0, "silent": 0, "too late": 2 * DAY, "slow": 3 * HOUR}


def _flatten(error: BaseException) -> str:
    """MCP wraps failures in nested task groups, so the message is several
    levels down and `str()` on the outer group says only that it exists."""
    if isinstance(error, BaseExceptionGroup):
        return " | ".join(_flatten(inner) for inner in error.exceptions)
    return f"{type(error).__name__}: {error}"


# --------------------------------------------------------------------------- #
# The four reviewers.
# --------------------------------------------------------------------------- #


# (why, which reviewer, turns, seconds a turn, their record, refunds, left queued)
REVIEWERS = [
    ("says yes, and the world moves", "grants", 3, HOUR, "granted", 1, 0),
    ("says no, and nothing moves", "denies", 3, HOUR, "denied", 0, 0),
    ("walks away, and it waits", "silent", 3, HOUR, "waiting", 0, 1),
    ("answers after the window, and is refused", "too late", 3, DAY, "refused", 0, 0),
    # Two turns, a reviewer who takes three hours: the customer asks twice and
    # both times there is genuinely nothing to tell them.
    ("takes longer than the customer waits", "slow", 2, HOUR, "waiting", 0, 1),
]


@pytest.mark.parametrize(
    ("why", "reviewer", "turns", "step_s", "outcome", "refunds", "queued"),
    REVIEWERS,
    ids=[r[0] for r in REVIEWERS],
)
@pytest.mark.discharges("AHC-0057", "AAC-0078", "P-REFUND", "ext:approval_queue", "op:issue_refund")
async def test_the_four_reviewers(
    why: str, reviewer: str, turns: int, step_s: int, outcome: str, refunds: int, queued: int
) -> None:
    """The four things a real reviewer does, and what each costs.

    *Yes* moves the money — **F-013**, which used to crash here: the stored
    arguments are the harness tool's and the executing tool is projected from
    the world, so the two are bound at execution rather than replayed. *No* must
    move nothing. *Walking away* is the common case in any operations queue and
    leaves an approval nobody is alerted about. *Too late* is a third outcome,
    distinct from yes and no: the reviewer says yes and is told the authority is
    gone, because treating it as a grant would refund on a decision nobody made
    about today's facts.
    """
    async with conversation(reviewer, turns=turns, step_s=step_s) as (world, record, approver):
        left = len(await approver.store.pending())

    outcomes = [r.outcome for r in approver.reviewed]
    assert outcome in outcomes, outcomes
    if outcome == "refused":
        assert "expired" in next(r.detail for r in approver.reviewed if r.outcome == "refused")
    assert len([e for e in world.effects if e[0] == "issue_refund"]) == refunds, world.effects
    assert left == queued
    if refunds:
        assert world.get("order", ORDER)["status"] == "refunded"


@pytest.mark.discharges("AHC-0037")
async def test_a_refund_the_world_cannot_take_fails_with_something_readable() -> None:
    """The other half of F-013, and the reason it raises rather than guesses.

    Two spare arguments and one empty required slot is a coin toss, and a coin
    toss on the refund path is worse than a stop. What the customer gets is a
    typed failure; what the operator gets is a sentence naming the tool, the
    argument it wanted and what was on offer.
    """
    from support_agent.contracts import Unbindable
    from support_agent.contracts import bind_arguments as _bind

    class Spec:
        name = "issue_refund"
        input_schema = {"properties": {"id": {}}, "required": ["id"]}

    with pytest.raises(Unbindable, match="issue_refund"):
        _bind(Spec(), {"reference": "AB-1", "amount": "24000"})


@pytest.mark.discharges("AHC-0057", "AAC-0056")
async def test_a_reviewer_cannot_approve_their_own_customer_s_request() -> None:
    """The confused deputy of the human path, driven by an actor rather than a
    direct call — a reviewer whose account *is* the customer's."""
    async with conversation("grants", by="C-1042") as (world, record, approver):
        pass

    refused = [r for r in approver.reviewed if r.outcome == "refused"]
    assert refused, [str(r) for r in approver.reviewed]
    assert "cannot be granted by the customer" in refused[0].detail
    assert world.effects == []


# --------------------------------------------------------------------------- #
# Timing.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_the_reviewer_is_a_declared_actor() -> None:
    """It has a determinism class like any other actor, because a run is only as
    reproducible as its weakest participant."""
    approver = Approver.grants(durable.Remembered(), _never_decides)
    assert approver.determinism.value == "scripted"


@pytest.mark.tooling
async def test_reviewing_an_empty_queue_is_silent() -> None:
    approver = Approver.grants(durable.Remembered(), _never_decides)
    assert await approver.review(at=1) == ()


async def _never_decides(*args: object, **kwargs: object) -> None:
    raise AssertionError("nothing to decide")
