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


async def test_a_granted_refund_cannot_execute_against_a_projected_world() -> None:
    """**F-013, pinned.** The reviewer says yes and the agent then crashes.

    `issue_refund` as projected from the world takes the entity's declared key,
    `id`. `request_refund` is harness-local, hard-codes `order_id`, and stores
    its arguments verbatim — so resumption replays `{order_id, amount}` into a
    tool that declares neither, and `jsonschema.validate` raises.

    The agent already has the fix for this. `_bind` exists, and its docstring is
    this defect word for word: *"the router knows it found an order id; it does
    not know what this world calls that field."* It is called on the
    deterministic route and **not** on the resume path. F-005 was repaired where
    it was found rather than everywhere its class lives.

    Written as an expected raise rather than left failing, so the defect is a
    fact in the suite. It is severe: the grant is recorded, the elevated identity
    is minted, and *then* it dies — the one path where the money moves.
    """
    approver = Approver.grants(store(), ap.decide)

    with pytest.raises(BaseExceptionGroup) as raised:
        await conversation(approver)

    assert "is a required property" in _flatten(raised.value)
    assert [r.outcome for r in approver.reviewed if r.outcome != "waiting"] == ["granted"], (
        "the human did their part — the failure is entirely on our side of the gate"
    )


async def test_a_reviewer_who_denies_produces_no_refund() -> None:
    """The customer must be told something true — and the world must not move."""
    approver = Approver.denies(store(), ap.decide)
    world, record = await conversation(approver)

    assert "denied" in [r.outcome for r in approver.reviewed]
    assert world.effects == [], "a denial that still refunded would be the worst outcome"


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


async def test_a_slow_reviewer_is_still_waiting_when_the_customer_follows_up() -> None:
    """An hour per turn, a reviewer who takes three. The customer asks twice and
    both times there is genuinely nothing to tell them."""
    approver = Approver.grants(store(), ap.decide, delay_s=3 * HOUR)
    world, record = await conversation(approver, turns=2, step_s=HOUR)

    assert {r.outcome for r in approver.reviewed} == {"waiting"}
    assert world.effects == []


def test_the_reviewer_is_a_declared_actor() -> None:
    """It has a determinism class like any other actor, because a run is only as
    reproducible as its weakest participant."""
    approver = Approver.grants(store(), ap.decide)
    assert approver.determinism.value == "scripted"


async def test_reviewing_an_empty_queue_is_silent() -> None:
    approver = Approver.grants(store(), ap.decide)
    assert await approver.review(at=1) == ()
