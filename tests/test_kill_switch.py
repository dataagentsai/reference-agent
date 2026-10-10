"""claims-fnol-azure A13 — a kill switch per agent, read through the config port.

`entrypoint.switch.Switched` wraps any `TurnAgent`. One table drives a sequence
of turns while the owner flips `agent.enabled`, and holds each turn to what the
switch promises: enabled, the agent runs; disabled, it does not, the turn is the
paused reply, the message is kept, and the turn's span and the turn counter say
so; flipped back, the agent runs again once the port's TTL has passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_harness import telemetry as tel
from agent_harness.config.settings import Cached
from agent_harness.contracts import Completed, Identity, Refused, TurnResult
from agent_harness.entrypoint.switch import ENABLED, PAUSED, RULE, Switched
from agent_harness.state import Conversation, InMemoryCheckpointStore
from agent_harness.telemetry.names import AGENT_ENABLED

ME = Identity(customer_id="C-1042")


@dataclass
class Counting:
    """A `TurnAgent` that answers every turn and counts them: the model's stand-in."""

    store: InMemoryCheckpointStore = field(default_factory=InMemoryCheckpointStore)
    escalations: None = None
    turns: int = 0

    async def opening(self, identity: Identity) -> str:
        return "Hello."

    async def handle(
        self, text: str, *, identity: Identity, conversation: Conversation | None = None, **_: Any
    ) -> tuple[TurnResult, Conversation]:
        self.turns += 1
        held = conversation or Conversation(conversation_id="conv-1", customer_id="C-1042")
        return Completed(reply="Answered."), held


def total(name: str, **labels: str) -> float:
    return sum(
        getattr(point, "value", 0)
        for attributes, point in tel.metric_points(name)
        if all(attributes.get(k) == v for k, v in labels.items())
    )


# (step, what the store holds, seconds since the last step, the agent runs, the reply)
STEPS = [
    ("enabled: the agent runs", "true", 0, True, "Answered."),
    ("disabled, within the TTL: still running", "false", 10, True, "Answered."),
    ("disabled, after the TTL: paused", "false", 30, False, PAUSED),
    ("still disabled: paused again", "false", 1, False, PAUSED),
    ("enabled again, within the TTL: still paused", "true", 1, False, PAUSED),
    ("enabled again, after the TTL: the agent runs", "true", 30, True, "Answered."),
]


@pytest.mark.discharges("AAC-0055", "AHC-0003")
async def test_the_switch_stops_every_new_turn_and_lets_them_through_again() -> None:
    exporter = tel.configure()
    now = [0.0]
    held = {"agent.enabled": "true"}
    settings = Cached(
        [ENABLED],
        lambda names: {n: held[n] for n in names if n in held},
        ttl_s=30.0,
        clock=lambda: now[0],
    )
    inner = Counting()
    switched = Switched(inner, settings)
    conversation = Conversation(conversation_id="conv-1", customer_id="C-1042")
    for step, value, elapsed, runs, reply in STEPS:
        held["agent.enabled"] = value
        now[0] += elapsed
        before = inner.turns
        result, conversation = await switched.handle(
            f"message for {step}", identity=ME, conversation=conversation
        )
        assert (inner.turns > before) is runs, step
        assert getattr(result, "reply", "") == reply, step
        if not runs:
            assert isinstance(result, Refused) and result.rule_id == RULE
            assert conversation.messages[-2].content == f"message for {step}", "the message is kept"
            assert await inner.store.latest("conv-1") is not None
    paused = [tel.attributes_of(s) for s in exporter.get_finished_spans() if s.name == "agent.turn"]
    assert [a[AGENT_ENABLED] for a in paused] == [False, False, False]
    assert total("agent.turns", result="paused") == 3


@pytest.mark.discharges("AHC-0003")
async def test_with_the_switch_off_the_opening_is_the_paused_reply_too() -> None:
    off = Cached([ENABLED], lambda _names: {"agent.enabled": "false"})
    assert await Switched(Counting(), off).opening(ME) == PAUSED
    on = Cached([ENABLED], lambda _names: {})
    assert await Switched(Counting(), on).opening(ME) == "Hello."
