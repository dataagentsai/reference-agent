"""The two release gates that had no test behind them.

The conformance report said it plainly:

    RELEASE GATES WITH NO TEST BEHIND THEM (2)
      AAC-0092  Streamed output is screened before it reaches the caller
      AAC-0096  Cached responses never cross a trust boundary

Both are guardrails, both were declared must-pass for release, and neither had a
single assertion. That is worse than an untested non-gate: the manifest asserted
coverage that did not exist, so the report was reassuring about the two things it
should have been loudest about.

Doc 31 is why they came first. AAC-0092 is theoretical for nine of the ten agents
examined there and **load-bearing** for the tenth — a voice agent streams, so
tokens reach the caller before any screen sees them. Writing the test when a
voice agent appears would be writing it a release too late.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from support_agent import identity as ident
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent.cassette import (
    Cassette,
    Context,
    Exchange,
    Player,
    Recorder,
    TrustBoundaryCrossed,
)
from support_agent.contracts import Identity, LLMClient, ModelResponse
from support_agent.llm import GroqClient, ScriptedClient, UnavailableClient

CASSETTE = Path(__file__).parent.parent / "cassettes" / "first_real_call.json"

STRONG = Context(model="openai/gpt-oss-120b", tools=("get_order",))
WEAK = Context(model="openai/gpt-oss-20b", tools=("get_order",))
ELEVATED = Context(model="openai/gpt-oss-120b", tools=("get_order", "issue_refund"))


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def says(text: str, *, model: str = "") -> ModelResponse:
    return ModelResponse(text=text, model=model)


# --------------------------------------------------------------------------- #
# AAC-0096 · Cached responses never cross a trust boundary.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("why", "recorded", "replaying"),
    [
        ("a different model — F-010", STRONG, WEAK),
        ("a wider tool surface than was recorded", STRONG, ELEVATED),
        ("a narrower one", ELEVATED, STRONG),
        (
            "the same model at a different temperature",
            Context(model="m", temperature=0.0),
            Context(model="m", temperature=0.9),
        ),
    ],
)
@pytest.mark.discharges("AAC-0096")
def test_a_recording_refuses_a_foreign_configuration(
    why: str, recorded: Context, replaying: Context
) -> None:
    cassette = Cassette([Exchange("fp", says("recorded"))], context=recorded)

    with pytest.raises(TrustBoundaryCrossed):
        Player(cassette, expect=replaying)


@pytest.mark.discharges("AAC-0096")
def test_a_recording_refuses_a_replay_that_declares_nothing() -> None:
    """Fails closed in the other direction too.

    *"The caller did not say what they were running under"* is not a reason to
    assume it matches. Refusing to answer is the point of a trust boundary.
    """
    cassette = Cassette([Exchange("fp", says("recorded"))], context=STRONG)

    with pytest.raises(TrustBoundaryCrossed, match="did not say"):
        Player(cassette)


def test_the_matching_configuration_replays() -> None:
    cassette = Cassette([Exchange("fp", says("recorded"))], context=STRONG)
    assert isinstance(Player(cassette, expect=STRONG), LLMClient)


def test_the_refusal_is_still_a_cassette_miss() -> None:
    """Its own type so a suite can distinguish *nothing answers this* from
    *something answers this and must not be used* — and a subclass so existing
    handling keeps working."""
    from support_agent.cassette import CassetteMiss

    assert issubclass(TrustBoundaryCrossed, CassetteMiss)


@pytest.mark.discharges("AAC-0096")
async def test_a_recorder_refuses_a_context_the_provider_contradicts() -> None:
    """A declared context the provider disagrees with is worse than none.

    Caught at record time, where somebody is present to fix it, rather than at
    replay time against a claim that was false when it was written.
    """
    recorder = Recorder(ScriptedClient([says("hello", model="openai/gpt-oss-20b")]), context=STRONG)

    with pytest.raises(TrustBoundaryCrossed, match="answered"):
        await recorder.complete(_request())


@pytest.mark.discharges("AAC-0096")
def test_the_committed_recording_declares_what_made_it() -> None:
    """The real cassette from the first live call, migrated to format 2.

    Asserted against the file rather than a fixture: a gate that only ever sees
    constructed objects is a gate that has never met the artifact it protects.
    """
    cassette = Cassette.load(CASSETTE)

    assert cassette.context, "a recording that declares nothing can never be checked"
    assert "openai/gpt-oss-120b" in cassette.context
    assert len(cassette) == 2

    with pytest.raises(TrustBoundaryCrossed):
        Player(cassette, expect=WEAK)


# --------------------------------------------------------------------------- #
# AAC-0092 · Streamed output is screened before it reaches the caller.
# --------------------------------------------------------------------------- #

CLIENTS = [GroqClient, ScriptedClient, UnavailableClient, Player, Recorder]


@pytest.mark.parametrize("client", CLIENTS, ids=lambda c: c.__name__)
@pytest.mark.discharges("AAC-0092")
def test_no_client_can_emit_before_the_reply_is_complete(client: type) -> None:
    """The gate, and it is structural on purpose.

    We do not stream today, so the obligation is *vacuously* satisfied — and
    F-005 is the standing lesson about exactly that: an obligation marked
    discharged by a test that drove a configuration where the defect could not
    appear. A test asserting "we are safe because we do not do the dangerous
    thing" has to also assert that **starting** to do it trips the gate.

    So: every client returns one complete `ModelResponse`. Adding `stream()` or
    making `complete()` an async generator fails this test, which is the point —
    whoever adds streaming is made to answer the obligation rather than
    inheriting a green report from a manifest that was never checked.
    """
    emitters = [
        name
        for name, _ in inspect.getmembers(client, callable)
        if not name.startswith("_") and name in {"stream", "stream_complete", "iter", "astream"}
    ]
    assert emitters == [], f"{client.__name__} can emit incrementally: {emitters}"

    complete = client.complete
    assert not inspect.isasyncgenfunction(complete), (
        f"{client.__name__}.complete yields — output escapes before policy sees it"
    )


@pytest.mark.discharges("AAC-0092")
async def test_policy_screens_the_whole_reply_and_can_still_block_it() -> None:
    """The invariant underneath the obligation, not just the absence of streaming.

    What AAC-0092 is *about* is that nothing reaches the caller unscreened. Here
    the screen sees the complete text and blocks — which is only possible because
    the text was complete. Under streaming the first half is already spoken.
    """
    verdict = pol.enforce(
        pol.Context(
            position=pol.Position.POST_MODEL,
            identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
            text="I have cancelled that order for you.",
            tool_results=(),
        )
    )

    assert verdict.blocked, "an unclaimed effect must not reach the customer"
    assert verdict.rule == "no_unclaimed_effect"


def _request():
    from support_agent.contracts import Message, ModelRequest

    return ModelRequest(messages=(Message(role="user", content="hello"),))
