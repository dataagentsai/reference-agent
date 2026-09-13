"""AHC-0108 — the record beside the transcript, and the handoff built from it.

The transcript is a record of sentences. Everything anybody later asks of a
piece of work — which order, what was actually done, who is holding it — is
answerable from it only by reading prose the model wrote, and only for as long
as the prose survives a reduction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent.contracts import Identity, ModelResponse, ToolCall, Usage
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore
from support_agent.state.facts import Facts
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
PENDING = "AB-10002"


def caller() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


# (name, what happens to the record, what it should then hold)
BUILDING = [
    ("nothing yet", lambda f: f, Facts()),
    ("what they asked", lambda f: f.asking("  cancel AB-10002  "), Facts(asked="cancel AB-10002")),
    (
        "the latest question wins",
        lambda f: f.asking("where is it").asking("cancel it"),
        Facts(asked="cancel it"),
    ),
    ("a record touched", lambda f: f.touching("AB-2"), Facts(records=("AB-2",))),
    (
        "records are sorted, so two runs compare",
        lambda f: f.touching("AB-9").touching("AB-1"),
        Facts(records=("AB-1", "AB-9")),
    ),
    (
        "the same record nine times costs one entry",
        lambda f: f.touching("AB-1").touching("AB-1").touching("AB-1"),
        Facts(records=("AB-1",)),
    ),
    (
        "an effect landed",
        lambda f: f.landed("cancel_order", "AB-1"),
        Facts(done=("cancel_order:AB-1",)),
    ),
    (
        "waiting on somebody",
        lambda f: f.waiting_on("approval", "apr_1"),
        Facts(awaiting=("approval:apr_1",)),
    ),
    (
        "and they answered",
        lambda f: f.waiting_on("approval", "apr_1").settled("approval", "apr_1"),
        Facts(),
    ),
]


@pytest.mark.discharges("AHC-0108")
@pytest.mark.parametrize(("name", "build", "expected"), BUILDING, ids=[c[0] for c in BUILDING])
def test_what_the_record_holds(name: str, build, expected: Facts) -> None:
    """Bounded by kind rather than by age — which is what lets the transcript be
    reduced while this is not. Nothing here is a list that grows with the
    conversation, so there is no oldest entry to drop."""
    assert build(Facts()) == expected


@pytest.mark.discharges("AHC-0108", "AHC-0070")
def test_the_handoff_is_assembled_rather_than_summarised() -> None:
    """Every line traceable to something that happened, so nothing in it is a
    paraphrase and nothing could have been lost to a reduction."""
    facts = (
        Facts()
        .asking("please cancel AB-10002")
        .touching(PENDING)
        .landed("cancel_order", PENDING)
        .waiting_on("escalation", "E-7")
    )
    handed = facts.as_handoff()
    assert "please cancel AB-10002" in handed
    assert PENDING in handed
    assert "cancel_order:AB-10002" in handed
    assert "E-7" in handed


@pytest.mark.discharges("AHC-0108")
def test_an_empty_record_says_so_rather_than_saying_nothing() -> None:
    """A blank handoff and a handoff about a conversation where nothing happened
    look identical to the person receiving one, and only the second is true."""
    handed = Facts().as_handoff()
    assert "Nothing asked yet" in handed
    assert "Nothing done yet" in handed


@pytest.mark.discharges("AHC-0108", "AAC-0062")
async def test_the_record_is_written_from_what_happened_not_what_was_said() -> None:
    """The fact-versus-claim distinction, end to end.

    The model cancels one order and *says* it cancelled a different one. What
    lands in the record is the first, because the record is written where the
    far system confirmed the effect and not from the sentence describing it.
    """
    world = Live.start(load(WORLD))
    act = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    claim = ModelResponse(
        text="I have cancelled AB-99999 for you.", usage=Usage(input_tokens=5, output_tokens=2)
    )

    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([act, claim, claim]),
            tools=tools,
            store=InMemoryCheckpointStore(),
        )
        _, conversation = await agent.handle(f"please cancel {PENDING}", identity=caller())

    assert conversation.facts.records == (PENDING,), conversation.facts
    assert conversation.facts.done == (f"cancel_order:{PENDING}",), conversation.facts
    assert "AB-99999" not in str(conversation.facts), "a claim reached the record"


@pytest.mark.discharges("AHC-0108", "AHC-0070", "op:escalate")
async def test_the_colleague_is_handed_the_record_not_the_transcript() -> None:
    """What the escalation carries. An escalation that makes the customer repeat
    everything is the moment an assistant becomes worse than no assistant."""
    world = Live.start(load(WORLD))
    escalations = esc.InMemoryEscalationStore()
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            escalations=escalations,
        )
        result, _ = await agent.handle("I want to speak to a human", identity=caller())

    raised = await escalations.get(result.ticket_id)
    assert raised is not None
    assert "I want to speak to a human" in raised.context, raised.context


@pytest.mark.discharges("AHC-0108", "AHC-0057")
async def test_a_pending_approval_is_in_the_record_before_anybody_reads_it() -> None:
    """The thing a transcript states least reliably, because the sentence that
    mentions it is the one most likely to have been compacted away."""
    world = Live.start(load(WORLD))
    approvals = ap.InMemoryApprovalStore()
    plan = ModelResponse(
        tool_calls=(ToolCall(id="c1", name="request_refund", arguments={"order_id": "AB-10003"}),),
        usage=Usage(input_tokens=5, output_tokens=2),
    )
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([plan]),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
        )
        result, conversation = await agent.handle("refund AB-10003", identity=caller())

    assert conversation.facts.awaiting == (f"approval:{result.approval_id}",), conversation.facts
