"""Where a rule becomes real.

The first controls in this system that inspect what the model *said*. Until now
the router guarded input and the tool boundary guarded action, and nothing sat
between the model and the customer.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import policy as pol
from support_agent import telemetry as tel
from support_agent.contracts import (
    Identity,
    ModelResponse,
    SideEffectClass,
    TerminationReason,
    ToolCall,
    ToolResult,
)
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.tools import META_REQUIRED_SCOPE, META_SIDE_EFFECT, connect


class OrderOut(BaseModel):
    order_id: str
    status: str


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def customer() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def reply(text: str, *results: ToolResult) -> pol.Context:
    return pol.Context(
        position=pol.Position.POST_MODEL,
        identity=customer(),
        text=text,
        tool_results=results,
    )


def refunded() -> ToolResult:
    return ToolResult(name="issue_refund", structured={"refund_id": "rf_1"})


# --------------------------------------------------------------------------- #
# The rule that matters most: claiming something that did not happen.
# --------------------------------------------------------------------------- #

CLAIM_CASES = [
    ("plain claim", "I have refunded your order.", True),
    ("passive claim", "Your refund has been processed.", True),
    ("past tense", "I refunded it this morning.", True),
    ("issued", "I issued your refund.", True),
    ("promise, not a claim", "I will ask a colleague to refund this.", False),
    ("explaining policy", "Refunds take five to seven working days.", False),
    ("plain status", "Your order has shipped.", False),
]


@pytest.mark.parametrize(("name", "text", "blocked"), CLAIM_CASES, ids=[c[0] for c in CLAIM_CASES])
@pytest.mark.discharges("AAC-0003")
def test_a_refund_may_only_be_claimed_if_one_happened(name: str, text: str, blocked: bool) -> None:
    """The gate stops an unauthorised refund; this stops the agent saying it did
    one anyway. Both cost the same at the support desk, and only one shows up in
    the ledger."""
    assert pol.enforce(reply(text)).blocked is blocked


def test_the_same_claim_is_allowed_when_the_refund_really_happened() -> None:
    text = "I have refunded your order."
    assert pol.enforce(reply(text)).blocked
    assert not pol.enforce(reply(text, refunded())).blocked


def test_a_failed_refund_does_not_license_the_claim() -> None:
    failed = ToolResult(name="issue_refund", is_error=True, error_channel="execution")
    assert pol.enforce(reply("I have refunded your order.", failed)).blocked


# --------------------------------------------------------------------------- #
# The other output rules.
# --------------------------------------------------------------------------- #

OUTPUT_CASES = [
    ("invented date", "It will arrive on 2026-09-14.", (), True),
    ("supported date", "It will arrive on 2026-09-14.", ("2026-09-14",), False),
    ("card echoed", "I can see the card ending 4111111111111111.", (), True),
    ("discount offered", "I can give you 10% off for the trouble.", (), True),
    ("voucher offered", "Here is a voucher for next time.", (), True),
    ("ordinary reply", "Your parcel is with the courier.", (), False),
]


@pytest.mark.parametrize(
    ("name", "text", "evidence", "blocked"),
    OUTPUT_CASES,
    ids=[c[0] for c in OUTPUT_CASES],
)
@pytest.mark.discharges("AAC-0006", "AAC-0005")
def test_output_rules(name: str, text: str, evidence: tuple[str, ...], blocked: bool) -> None:
    results = tuple(ToolResult(name="get_order", structured={"eta": e}) for e in evidence)
    assert pol.enforce(reply(text, *results)).blocked is blocked


def test_a_discount_the_customer_never_asked_for_is_still_refused() -> None:
    """The half of the refusal list the router cannot reach: the customer did not
    ask, the model volunteered."""
    verdict = pol.enforce(reply("I can give you 20% off."))
    assert verdict.rule == "no_discount_offer"


# --------------------------------------------------------------------------- #
# Fails closed. AAC-0091.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0091")
def test_a_rule_that_raises_blocks_the_traffic() -> None:
    """A guardrail that errors open is believed and absent at the same time, and
    nobody goes looking for a control they think they have."""

    def explodes(ctx: pol.Context) -> pol.Verdict:
        raise RuntimeError("detector is down")

    verdict = pol.enforce(reply("anything at all"), rules=(explodes,))
    assert verdict.blocked
    assert "blocked" in verdict.reason


def test_the_failure_is_visible_on_the_trace(exporter) -> None:
    def explodes(ctx: pol.Context) -> pol.Verdict:
        raise RuntimeError("boom")

    pol.enforce(reply("anything"), rules=(explodes,))
    span = next(s for s in exporter.get_finished_spans() if s.name == "agent.policy")
    attrs = tel.attributes_of(span)
    assert attrs["agent.policy.errored"] is True
    assert attrs["agent.policy.blocked_by"] == "explodes"


def test_first_block_wins_and_later_rules_do_not_run() -> None:
    ran: list[str] = []

    def blocks(ctx: pol.Context) -> pol.Verdict:
        ran.append("blocks")
        return pol.block("blocks", "no")

    def never(ctx: pol.Context) -> pol.Verdict:
        ran.append("never")
        return pol.ALLOW

    assert pol.enforce(reply("x"), rules=(blocks, never)).blocked
    assert ran == ["blocks"]


def test_an_empty_rule_set_allows() -> None:
    assert not pol.enforce(reply("x"), rules=()).blocked


# --------------------------------------------------------------------------- #
# Through the loop.
# --------------------------------------------------------------------------- #


@pytest.fixture
def server():
    srv = MCPServer("ecom")
    srv.state = {"refunds": 0}  # type: ignore[attr-defined]

    @srv.tool(meta={META_SIDE_EFFECT: SideEffectClass.READ.value})
    def get_order(order_id: str) -> OrderOut:
        """Look up an order."""
        return OrderOut(order_id=order_id, status="shipped")

    @srv.tool(
        meta={
            META_SIDE_EFFECT: SideEffectClass.IRREVERSIBLE.value,
            META_REQUIRED_SCOPE: ident.SCOPE_REFUNDS_WRITE,
        }
    )
    def issue_refund(order_id: str) -> OrderOut:
        """Refund. Not reachable by a customer session."""
        srv.state["refunds"] += 1  # type: ignore[attr-defined]
        return OrderOut(order_id=order_id, status="refunded")

    return srv


@pytest.mark.discharges("AAC-0003", "AAC-0005")
async def test_a_false_claim_never_reaches_the_customer(server) -> None:
    """End to end: the model lies, the customer does not hear it."""
    async with connect(server, ledger=InMemoryLedger()) as tools:
        result, trace = await agent_loop.run(
            "refund me",
            identity=customer(),
            llm=ScriptedClient([ModelResponse(text="I have refunded your order.")]),
            tools=tools,
            system_prompt="s",
        )
    assert result.reply == pol.SAFE_REPLY
    assert "refunded" not in result.reply
    assert trace.termination is TerminationReason.REFUSED
    assert server.state["refunds"] == 0


async def test_a_truthful_reply_passes_through_untouched(server) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        result, trace = await agent_loop.run(
            "where is AB-1",
            identity=customer(),
            llm=ScriptedClient(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(id="t", name="get_order", arguments={"order_id": "AB-1"}),
                        )
                    ),
                    ModelResponse(text="Your order has shipped."),
                ]
            ),
            tools=tools,
            system_prompt="s",
        )
    assert result.reply == "Your order has shipped."
    assert trace.termination is TerminationReason.GOAL_REACHED


async def test_the_blocking_rule_is_named_on_the_trace(server, exporter) -> None:
    async with connect(server, ledger=InMemoryLedger()) as tools:
        await agent_loop.run(
            "refund me",
            identity=customer(),
            llm=ScriptedClient([ModelResponse(text="Your refund has been issued.")]),
            tools=tools,
            system_prompt="s",
        )
    run = next(s for s in exporter.get_finished_spans() if s.name == "agent.run")
    assert tel.attributes_of(run)["agent.policy.blocked_by"] == "no_unclaimed_effect"


# --------------------------------------------------------------------------- #
# F-004 — a guardrail that blocks correct behaviour is worse than one that
# misses a claim, because it fires constantly and is therefore switched off.
# --------------------------------------------------------------------------- #

HONEST_REFUSALS = [
    (
        "explaining a cancellation cannot happen",
        "That order has shipped, so it can no longer be cancelled.",
    ),
    ("cancellation not possible", "Unfortunately this order cannot be cancelled now."),
    ("refund not possible", "That purchase cannot be refunded — it was a final sale item."),
    ("describing the policy", "Orders can be cancelled up until they are picked."),
    ("offering the alternative", "I cannot cancel it, but you can refuse delivery."),
    ("asking a question", "Would you like me to cancel it?"),
]


@pytest.mark.parametrize(("name", "text"), HONEST_REFUSALS, ids=[c[0] for c in HONEST_REFUSALS])
def test_an_honest_refusal_is_not_mistaken_for_a_claim(name: str, text: str) -> None:
    assert not pol.enforce(reply(text)).blocked, f"blocked a correct refusal: {text!r}"


AFFIRMATIVE_CLAIMS = [
    ("cancelled, first person", "I have cancelled that order for you."),
    ("cancelled, passive", "Your order has been cancelled."),
    ("cancelled, bare past", "I cancelled it this morning."),
    ("replacement sent", "I have dispatched a replacement."),
]


@pytest.mark.parametrize(
    ("name", "text"), AFFIRMATIVE_CLAIMS, ids=[c[0] for c in AFFIRMATIVE_CLAIMS]
)
def test_an_affirmative_claim_is_still_caught(name: str, text: str) -> None:
    assert pol.enforce(reply(text)).blocked


def test_a_refused_tool_result_does_not_license_the_claim() -> None:
    """`allowed: false` is not success. The tool ran and declined."""
    refused = ToolResult(name="cancel_order", structured={"allowed": False, "reason": "shipped"})
    assert pol.enforce(reply("I have cancelled that order.", refused)).blocked


def test_a_successful_tool_result_does_license_it() -> None:
    ok = ToolResult(name="cancel_order", structured={"allowed": True, "reason": "allowed"})
    assert not pol.enforce(reply("I have cancelled that order.", ok)).blocked
