"""F-035 / AHC-0106 — a reply that commits to work nothing is doing.

The mirror of abstention. A turn ending as `Completed` has, by that type's own
definition, nothing that will produce a later answer — so a `Completed` whose
words promise one is a state the system cannot honour, and the customer waits
for something nobody is doing.

Found by a reader watching the run view and asking why the agent said *"let me
check on that for you"* and never came back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project
from evals import durable

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent.contracts import (
    Completed,
    Escalated,
    Identity,
    RunId,
    TerminationReason,
    ToolCall,
)
from support_agent.entrypoint import promise
from support_agent.llm import ModelResponse, ScriptedClient, Usage
from support_agent.requests import InMemoryRequests
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
CUSTOMER = "C-1042"


def caller() -> Identity:
    return Identity(customer_id=CUSTOMER, scopes=ident.CUSTOMER_SCOPES)


def _conversation() -> Conversation:
    return Conversation(conversation_id="c-1", customer_id=CUSTOMER)


def says(text: str) -> ScriptedClient:
    """A model that answers in words and calls nothing — the whole shape of the
    defect, and also of a perfectly ordinary answer."""
    return ScriptedClient([ModelResponse(text=text, usage=Usage(input_tokens=5, output_tokens=3))])


# (what the reply says, the kind of commitment it makes or "" for none)
READINGS = [
    ("let me check", "Let me check that for you.", "first-person future act"),
    ("i will look", "I will look into this and sort it out.", "first-person future act"),
    ("i'll chase", "I'll chase that up with the warehouse.", "first-person future act"),
    ("we'll contact", "We will contact the courier about it.", "first-person future act"),
    ("get back to you", "Someone will get back to you shortly.", "will return with an answer"),
    ("let you know", "I will let you know as soon as I hear.", "will return with an answer"),
    ("one moment", "One moment while I pull that up.", "asks the customer to wait"),
    ("bear with me", "Bear with me a second.", "asks the customer to wait"),
    # Answers. Each states something already true, which is what an ending is.
    ("a plain answer", "Your order was delivered on the 3rd of March.", ""),
    ("a past action", "That order was still pending, so I have cancelled it.", ""),
    ("a refusal", "That order is outside the return window, so I cannot accept it.", ""),
    # Effort offered now, not an answer owed later. The distinction the table is
    # drawn around: `CAPPED_REPLY` and `LAPSED_REPLY` both end this way and both
    # are true, so a rule that caught "I will do what I can" would fire on the
    # agent's most honest sentences.
    ("effort, not an answer", "I will do what I can with that.", ""),
    ("an offer to try", "I will try to help you with this.", ""),
    # Said by the desk, and backed by the reference that follows it.
    ("the desk's own words", esc.RAISED_REPLY.format(ticket="E-1"), "first-person future act"),
    ("what is said instead", promise.WITHDRAWN_REPLY, ""),
    ("past the cap", esc.CAPPED_REPLY, ""),
    ("nobody came", esc.LAPSED_REPLY.format(ticket="E-1"), ""),
    ("no desk at all", esc.NO_DESK_REPLY, ""),
]


@pytest.mark.discharges("AHC-0106", "AAC-0112")
@pytest.mark.parametrize(
    ("name", "reply", "expected"), READINGS, ids=[case[0] for case in READINGS]
)
def test_what_counts_as_a_commitment(name: str, reply: str, expected: str) -> None:
    """The wording half, read on its own.

    `RAISED_REPLY` is in here *committing* on purpose. It opens with "Let me pass
    you to a colleague" and is the truest sentence the desk has, because the
    reference number follows it — which is exactly why the wording alone can
    never be the control. What saves it is the result type: it arrives as
    `Escalated`, and the gate never reads it.
    """
    assert promise.commits(reply) == expected


@pytest.mark.discharges("AHC-0106")
def test_what_is_said_instead_does_not_itself_promise() -> None:
    """A replacement that promised something would be the same defect with better
    manners. Asserted by running every constant the gate can substitute back
    through the rule that judged the original."""
    substitutes = [promise.WITHDRAWN_REPLY, esc.NO_DESK_REPLY, esc.CAPPED_REPLY]
    assert [promise.commits(text) for text in substitutes] == ["", "", ""]


@pytest.mark.discharges("AHC-0106", "AAC-0112", "AHC-0070")
async def test_a_promise_with_a_desk_becomes_a_handoff() -> None:
    """The promise is made true: a person is fetched and the customer gets the
    reference that proves it."""
    world = Live.start(load(WORLD))
    escalations = durable.RememberedEscalations()
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=says("Let me check that for you."),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=escalations,
        )
        result, _ = await agent.handle("please refund my order AB-10002", identity=caller())

    assert isinstance(result, Escalated), result
    assert result.ticket_id, "an escalation with no reference is the defect again"
    assert result.rule_id == promise.UNBACKED_PROMISE
    assert await escalations.get(result.ticket_id) is not None, "nothing was written down"


@pytest.mark.discharges("AHC-0106", "AAC-0112")
async def test_a_promise_with_nobody_to_fetch_is_withdrawn_not_relabelled() -> None:
    """No desk: the sentence goes, and nothing else does.

    The result keeps its type — the turn really did complete, badly — so a trace
    still says what happened. An earlier version returned the desk's refusal
    here, which removed the false promise and the record of the run with it.
    """
    world = Live.start(load(WORLD))
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=says("Let me check that for you."),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        result, _ = await agent.handle("please refund my order AB-10002", identity=caller())

    assert isinstance(result, Completed), result
    assert result.termination is TerminationReason.GOAL_REACHED
    assert promise.commits(result.reply) == "", result.reply
    assert world.effects == [], "nothing was done, which is the point"


@pytest.mark.discharges("AHC-0106")
async def test_an_ordinary_answer_is_left_exactly_as_it_was() -> None:
    """The gate must not fire on the common case, which is most of them."""
    answer = "Your order was delivered on the 3rd of March."
    world = Live.start(load(WORLD))
    escalations = durable.RememberedEscalations()
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=says(answer),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=escalations,
        )
        result, _ = await agent.handle("please refund my order AB-10002", identity=caller())

    assert isinstance(result, Completed) and result.reply == answer
    assert not await escalations.pending(), "a person was fetched for an ordinary answer"


@pytest.mark.discharges("AHC-0106", "AHC-0057")
async def test_a_turn_waiting_on_an_approval_is_never_touched() -> None:
    """`NeedsApproval` already names what will produce the answer, so the gate
    never reads it — and this test was written expecting to prove that by showing
    its wording *would* have tripped the rule.

    It does not, and the reason is worth keeping. `REFUND_WAIT_REPLY` is written
    in the past tense: *"I have sent this to a colleague to authorise. Nothing has
    been refunded yet."* It reports what was done and what was not, and commits
    to nothing — which is the same discipline this whole gate exists to enforce,
    arrived at independently when that constant was written. The protection is
    therefore double, and the type is the half that does not depend on anyone
    being careful again.
    """
    world = Live.start(load(WORLD))
    approvals = durable.Remembered()
    plan = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="request_refund", arguments={"id": "AB-10002"}),),
        usage=Usage(input_tokens=5, output_tokens=3),
    )
    async with connect(project(world), requests=InMemoryRequests()) as tools:
        agent = ep.build(
            llm=ScriptedClient([plan]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
        )
        result, _ = await agent.handle("refund order AB-10002 please", identity=caller())

    assert result.kind == "needs_approval", result
    assert result.reply == ap.REFUND_WAIT_REPLY
    assert promise.commits(result.reply) == "", "the wait reply commits to nothing, by its tense"

    # The half that does not depend on wording: hand the gate the same result
    # with words that plainly do promise, and it still must not touch it,
    # because an approval id is already the answer to "what will produce this".
    promising = result.model_copy(update={"reply": "Let me check that and come back to you."})
    untouched = await promise.honest(
        promising, ep.NoDesk(), _conversation(), caller(), RunId("r-1")
    )
    assert untouched is promising
