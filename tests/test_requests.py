"""One name, one outcome — the rule, and the three ways a name can end.

T-062. Two stores answered this question with opposite rules: the delivery claim
settled on failure, the ledger did not record one. These tests are what says
which answer is now the answer, at both scopes, because the point of merging
them was that the rule stops depending on where you are standing.
"""

from __future__ import annotations

import pytest

from support_agent import requests as req

pytestmark = pytest.mark.anyio


class Clock:
    """A clock a test can move, because the expiry is the whole subject."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


# (name, scope) — the rule is the same at both, which is the claim being made.
SCOPES = [("a delivery", req.Scope.DELIVERY), ("a tool call", req.Scope.TOOL)]


@pytest.mark.discharges("AAC-0076", "AHC-0053")
@pytest.mark.parametrize(("what", "scope"), SCOPES, ids=[s[0] for s in SCOPES])
async def test_a_definite_answer_is_returned_to_whoever_repeats_the_name(
    what: str, scope: req.Scope
) -> None:
    """The answer, not the news that one exists."""
    store = req.InMemoryRequests()
    async with req.once(store, "n-1", scope=scope) as claim:
        assert claim is not None
        claim.outcome = {"reply": "cancelled"}

    with pytest.raises(req.AlreadyAnswered) as refused:
        await store.claim("n-1", scope=scope)
    assert refused.value.outcome == {"reply": "cancelled"}


@pytest.mark.discharges("AAC-0076", "AHC-0053")
async def test_a_tool_call_with_nothing_to_report_leaves_the_name_free() -> None:
    """The row this module exists for.

    A timeout means it may or may not have happened, so nothing is stored and
    the name is owed — a retry goes out under the *same* name, which is the only
    thing that lets the far end tell a retry from a second request. Storing a
    guess here is how one refund becomes two.
    """
    store = req.InMemoryRequests()
    async with req.once(store, "n-2", scope=req.Scope.TOOL) as claim:
        assert claim is not None  # the body sets no outcome: it does not know

    retaken = await store.claim("n-2", scope=req.Scope.TOOL)
    assert retaken.name == "n-2"


@pytest.mark.discharges("AAC-0076", "AHC-0053")
async def test_a_delivery_with_nothing_to_report_is_still_answered() -> None:
    """The other half of the same rule, and the reason the scope decides it.

    A turn that reached the end ran, whatever it produced, and re-running it on
    redelivery would repeat whatever effects it managed before failing — the
    tool names that protected those effects carry a run id, and a second run
    does not share it. A process killed outright never reaches the end at all,
    and its claim is released by the expiry instead.
    """
    store = req.InMemoryRequests()
    async with req.once(store, "n-2b", scope=req.Scope.DELIVERY) as claim:
        assert claim is not None

    with pytest.raises(req.AlreadyAnswered):
        await store.claim("n-2b", scope=req.Scope.DELIVERY)


@pytest.mark.discharges("AAC-0076")
async def test_a_tool_call_that_raised_leaves_the_name_free() -> None:
    """An exception is not an answer, at the scope where arriving is not one."""
    store = req.InMemoryRequests()
    with pytest.raises(RuntimeError):
        async with req.once(store, "n-3", scope=req.Scope.TOOL):
            raise RuntimeError("the far end went away")

    assert (await store.claim("n-3", scope=req.Scope.TOOL)).name == "n-3"


@pytest.mark.discharges("AAC-0076")
async def test_two_holders_at_once_are_told_apart_from_a_repeat() -> None:
    """`StillRunning` and `AlreadyAnswered` are different types because the
    operator response differs: a repeat is routine, two holders is a race."""
    store = req.InMemoryRequests()
    await store.claim("n-4", scope=req.Scope.DELIVERY)

    with pytest.raises(req.StillRunning):
        await store.claim("n-4", scope=req.Scope.DELIVERY)


@pytest.mark.discharges("AAC-0076", "AHC-0053")
async def test_an_abandoned_claim_is_reclaimed_by_the_next_caller() -> None:
    """No sweeper, and no workflow. `finally` does not run for a killed process,
    so the claim is reclaimed by the next caller finding it stale rather than by
    anything going looking for it."""
    clock = Clock()
    store = req.InMemoryRequests(now=clock)
    await store.claim("n-5", scope=req.Scope.DELIVERY, ttl_s=600)

    with pytest.raises(req.StillRunning):
        await store.claim("n-5", scope=req.Scope.DELIVERY)

    clock.t += 601
    assert (await store.claim("n-5", scope=req.Scope.DELIVERY)).name == "n-5"


@pytest.mark.discharges("AAC-0076")
async def test_the_first_definite_answer_is_the_answer() -> None:
    """A later attempt under one name did not happen twice, so it must not be
    able to rewrite what did."""
    store = req.InMemoryRequests()
    await store.claim("n-6", scope=req.Scope.TOOL)
    await store.settle("n-6", {"reply": "first"})
    await store.settle("n-6", {"reply": "second"})

    with pytest.raises(req.AlreadyAnswered) as refused:
        await store.claim("n-6", scope=req.Scope.TOOL)
    assert refused.value.outcome == {"reply": "first"}


@pytest.mark.discharges("AHC-0053")
async def test_an_unnamed_request_runs_unguarded() -> None:
    """Stated rather than defaulted. Inventing a name here would produce a guard
    that can never fire and a green report to go with it."""
    store = req.InMemoryRequests()
    async with req.once(store, None, scope=req.Scope.TOOL) as claim:
        assert claim is None
    assert len(store) == 0


@pytest.mark.discharges("AHC-0053")
async def test_different_names_do_not_collide() -> None:
    store = req.InMemoryRequests()
    async with req.once(store, "n-7", scope=req.Scope.TOOL) as first:
        assert first is not None
        first.outcome = {"reply": "a"}
    async with req.once(store, "n-8", scope=req.Scope.TOOL) as second:
        assert second is not None
        second.outcome = {"reply": "b"}

    assert len(store) == 2
