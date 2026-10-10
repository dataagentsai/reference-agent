"""A crash mid-turn must not repeat a side effect (claims-fnol-azure Tier 4a, A5).

A turn cancels an order, the process dies before the checkpoint (so its delivery
claim is never settled), the claim expires, and the message is delivered again.
The retry is the same run, named by the delivery
(`agent_harness.entrypoint.persist.delivered`), so the cancel goes out under the
key it went out under first, and the order system answers it with its first
answer. Before A5 the retry ran under a fresh run id and a new key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from agenttwin import Live, load, project

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.contracts import (
    CLAIM_TTL_S,
    Claim,
    IdempotencyKey,
    Identity,
    ModelResponse,
    RunId,
    Scope,
    ToolCall,
    ToolClient,
    ToolRegistry,
    ToolResult,
)
from support_agent.entrypoint.persist import delivered
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
PENDING = "AB-10002"


def who() -> Identity:
    return Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)


def cancels() -> ScriptedClient:
    return ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="c1", name="cancel_order", arguments={"id": PENDING}),)
            ),
            ModelResponse(text="That has been cancelled."),
        ]
    )


class Crash(BaseException):
    """The process dying: nothing on the way out handles it."""


class Dies:
    durable = False

    async def checkpoint(self, *args: object, **kwargs: object) -> None:
        raise Crash("killed before the checkpoint")

    async def latest(self, conversation_id: str) -> bytes | None:
        return None


@dataclass
class Gone:
    """The ledger as a killed process leaves it: claimed, never settled."""

    inner: InMemoryRequests
    durable = False

    async def claim(self, name: str, *, scope: Scope, ttl_s: int = CLAIM_TTL_S) -> Claim:
        return await self.inner.claim(name, scope=scope, ttl_s=ttl_s)

    async def settle(self, name: str, outcome: dict[str, object] | None = None) -> None:
        return None

    async def abandon(self, name: str) -> None:
        return None


@dataclass
class Keys:
    inner: ToolClient
    sent: list[str] = field(default_factory=list)

    async def list_tools(self, identity: Identity) -> ToolRegistry:
        return await self.inner.list_tools(identity)

    async def call(
        self, name: str, arguments: dict[str, object], identity: Identity, key: IdempotencyKey
    ) -> ToolResult:
        if name == "cancel_order":
            self.sent.append(key.value)
        return await self.inner.call(name, arguments, identity, key)


@dataclass
class Monotonic:
    at: float = 1_000.0

    def __call__(self) -> float:
        return self.at


# [case, the second delivery id, same key]
REDELIVERIES = [
    ("the same message, redelivered after its claim expired", "msg-1", True),
    ("a new message", "msg-2", False),
]


@pytest.mark.discharges("AHC-0053", "AHC-0074", "AAC-0076")
@pytest.mark.parametrize(("case", "again", "same"), REDELIVERIES, ids=[r[0] for r in REDELIVERIES])
async def test_a_crash_before_the_checkpoint_repeats_no_key(
    case: str, again: str, same: bool
) -> None:
    world = Live.start(load(WORLD))
    clock = Monotonic()
    ledger = InMemoryRequests(now=clock)
    async with connect(project(world), requests=InMemoryRequests()) as projected:
        tools = Keys(projected)
        dying = ep.build(llm=cancels(), tools=tools, store=Dies(), deliveries=Gone(ledger))
        with pytest.raises(Crash):
            await dying.handle(f"cancel {PENDING}", identity=who(), delivery_id="msg-1")
        clock.at += CLAIM_TTL_S + 1
        restarted = ep.build(
            llm=cancels(), tools=tools, store=InMemoryCheckpointStore(), deliveries=ledger
        )
        await restarted.handle(f"cancel {PENDING}", identity=who(), delivery_id=again)

    first, second = tools.sent
    assert (first == second) is same, case
    assert [e for e in world.effects if e[0] == "cancel_order"] == [("cancel_order", PENDING)]


# [case, first (customer, delivery, conversation, part), second, same run]
NAMES = [
    ("the same message", ("C-1", "m", "", None), ("C-1", "m", "", None), True),
    ("another message", ("C-1", "m", "", None), ("C-1", "n", "", None), False),
    ("another part", ("C-1", "m", "", 1), ("C-1", "m", "", 2), False),
    ("another conversation", ("C-1", "m", "cnv_a", None), ("C-1", "m", "cnv_b", None), False),
    ("another customer", ("C-1", "m", "", None), ("C-2", "m", "", None), False),
]


@pytest.mark.discharges("AHC-0053", "AHC-0074")
@pytest.mark.parametrize(("case", "one", "two", "same"), NAMES, ids=[n[0] for n in NAMES])
def test_a_delivered_run_is_named_by_its_message(case: str, one: tuple, two: tuple, same: bool):
    def named(customer: str, delivery: str, conversation: str, part: int | None) -> RunId | None:
        held = (
            Conversation(conversation_id=conversation, customer_id=customer)
            if conversation
            else None
        )
        return delivered(customer, held, None, delivery, part=part)[1]

    first, second = named(*one), named(*two)
    assert first is not None and first.startswith("run_") and len(first) == 20
    assert (first == second) is same, case


@pytest.mark.discharges("AHC-0053")
def test_without_a_delivery_or_with_a_chosen_run_nothing_is_named() -> None:
    assert delivered("C-1", None, None, None) == (None, None)
    assert delivered("C-1", None, RunId("run_chosen"), "m") == (None, RunId("run_chosen"))
