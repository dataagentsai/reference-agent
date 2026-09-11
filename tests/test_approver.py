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

from pathlib import Path

import pytest
from agenttwin import Approver, Clock, Live, Scenario, ScriptedActor, load, project, run_scenario

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
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


async def conversation(approver, *, turns: int = 3, step_s: int = HOUR):
    """A customer who asks, then follows up, while a reviewer does or does not act."""
    world = Live.start(load(WORLD))
    scenario = Scenario(
        name="a refund that needs a human",
        max_turns=turns,
        predicates={
            "no refund without a grant": lambda w, t: True,  # replaced per test
        },
    )
    actor = ScriptedActor([f"please refund my order {ORDER}", "any update?", "any update?"][:turns])

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=asks_for_refund(),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approver.store,
        )
        record = await run_scenario(
            scenario,
            live=world,
            actor=actor,
            agent=agent,
            identity=who(),
            approver=approver,
            clock=Clock(step_s=step_s),
        )
    return world, record


def store() -> ap.InMemoryApprovalStore:
    return ap.InMemoryApprovalStore()


def _flatten(error: BaseException) -> str:
    """MCP wraps failures in nested task groups, so the message is several
    levels down and `str()` on the outer group says only that it exists."""
    if isinstance(error, BaseExceptionGroup):
        return " | ".join(_flatten(inner) for inner in error.exceptions)
    return f"{type(error).__name__}: {error}"


# --------------------------------------------------------------------------- #
# The four reviewers.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0057", "P-REFUND", "op:issue_refund")
async def test_a_granted_refund_executes_against_a_projected_world() -> None:
    """**F-013, fixed.** It used to crash here.

    `issue_refund` as projected from the world takes the entity's declared key,
    `id`. `request_refund` is harness-local, hard-codes `order_id`, and stores
    its arguments verbatim — so resumption replayed `{order_id, amount}` into a
    tool that declared neither, and `jsonschema.validate` raised through three
    nested task groups. The grant was recorded, the elevated identity was minted,
    and *then* it died: the one path where money moves.

    The fix is `_bind` on the resume path, which `_direct` had already been doing
    since F-005. The binding has to happen **here** rather than at request time,
    because `issue_refund` only appears on the elevated surface and the customer
    identity that raises the request cannot see it.
    """
    approver = Approver.grants(store(), ap.decide)
    world, record = await conversation(approver)

    assert [r.outcome for r in approver.reviewed if r.outcome != "waiting"] == ["granted"]
    assert ("issue_refund", ORDER) in world.effects, world.effects
    assert world.get("order", ORDER)["status"] == "refunded"


@pytest.mark.discharges("AHC-0037")
async def test_a_refund_the_world_cannot_take_fails_with_something_readable() -> None:
    """The other half of the fix, and the reason it raises rather than guesses.

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


@pytest.mark.discharges("AHC-0057")
async def test_a_reviewer_who_denies_produces_no_refund() -> None:
    """The customer must be told something true — and the world must not move."""
    approver = Approver.denies(store(), ap.decide)
    world, record = await conversation(approver)

    assert "denied" in [r.outcome for r in approver.reviewed]
    assert world.effects == [], "a denial that still refunded would be the worst outcome"


@pytest.mark.discharges("AHC-0057", "ext:approval_queue")
async def test_a_reviewer_who_walks_away_leaves_it_pending_forever() -> None:
    """The common case in any real operations queue, and the one never tested.

    Nothing happens, which is correct — and the point is that *nothing happens
    quietly*. The approval is still sitting there after the customer has given
    up, and no part of the system says so.
    """
    approver = Approver.silent(store(), ap.decide)
    world, record = await conversation(approver)

    assert {r.outcome for r in approver.reviewed} == {"waiting"}
    assert world.effects == []
    assert await approver.store.pending(), "still queued, and nobody is alerted"


@pytest.mark.discharges("AHC-0057", "AAC-0078")
async def test_a_reviewer_who_answers_after_the_window_is_refused() -> None:
    """The third outcome, distinct from yes and no.

    The reviewer says *yes* — and is told they are too late. A system that
    treated this as a grant would refund on a decision nobody made about today's
    facts; one that treated it as a denial would tell the customer their reviewer
    said no, which is untrue.
    """
    approver = Approver.grants(store(), ap.decide, delay_s=2 * DAY)
    world, record = await conversation(approver, turns=3, step_s=DAY)

    outcomes = [r.outcome for r in approver.reviewed]
    assert "refused" in outcomes, outcomes
    assert "expired" in next(r.detail for r in approver.reviewed if r.outcome == "refused")
    assert world.effects == []


@pytest.mark.discharges("AHC-0057", "AAC-0056")
async def test_a_reviewer_cannot_approve_their_own_customer_s_request() -> None:
    """The confused deputy of the human path, driven by an actor rather than a
    direct call — a reviewer whose account *is* the customer's."""
    approver = Approver.grants(store(), ap.decide, name="C-1042")
    world, record = await conversation(approver)

    refused = [r for r in approver.reviewed if r.outcome == "refused"]
    assert refused, [str(r) for r in approver.reviewed]
    assert "cannot be granted by the customer" in refused[0].detail
    assert world.effects == []


# --------------------------------------------------------------------------- #
# Timing.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AHC-0057", "ext:approval_queue")
async def test_a_slow_reviewer_is_still_waiting_when_the_customer_follows_up() -> None:
    """An hour per turn, a reviewer who takes three. The customer asks twice and
    both times there is genuinely nothing to tell them."""
    approver = Approver.grants(store(), ap.decide, delay_s=3 * HOUR)
    world, record = await conversation(approver, turns=2, step_s=HOUR)

    assert {r.outcome for r in approver.reviewed} == {"waiting"}
    assert world.effects == []


@pytest.mark.tooling
def test_the_reviewer_is_a_declared_actor() -> None:
    """It has a determinism class like any other actor, because a run is only as
    reproducible as its weakest participant."""
    approver = Approver.grants(store(), ap.decide)
    assert approver.determinism.value == "scripted"


@pytest.mark.tooling
async def test_reviewing_an_empty_queue_is_silent() -> None:
    approver = Approver.grants(store(), ap.decide)
    assert await approver.review(at=1) == ()
