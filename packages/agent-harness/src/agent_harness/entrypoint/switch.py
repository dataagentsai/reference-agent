"""A kill switch per agent (claims-fnol-azure A13): `agent.enabled`, read every turn.

An owner who sees an agent misbehave — spending, saying something it must not,
acting on something it should not — needs one setting that stops it now and
needs no deploy. This is that setting: a declared key on the `config` port,
`agent.enabled` (a bool, default true), read at the turn's entry and re-read
after the port's TTL, so `az appconfig kv set ... --value false` stops the next
turn after at most one TTL.

**When it is false, every new turn is the paused reply** and nothing else: no
model call, and no route either, the deterministic ones included. A switch is
pulled when something is wrong and not yet understood, and a stop that leaves
half the agent running is one the owner has to reason about under pressure; a
deterministic route also reads the claims system and raises escalations, which
are the agent acting. The policyholder's message is kept on the conversation,
so it is there when the agent is back and for whoever reads the transcript.

**What the switch does not touch.** Work already in a person's hands goes on:
an approval or escalation waiting at the desk is decided there, through the
desk's own routes, and its carry-out is a workflow, not a turn. A conversation's
pointer to its waiting approval is kept, so the turn after the agent is back
resumes it.

**Recorded.** Every turn's span carries the switch's state (`agent.enabled`)
when a switch is wired, and a paused turn is counted as
`agent.turns{result="paused"}`. Its result is a `Refused` with rule
`agent.paused`, so an edge answers it as it answers any refusal.

Generic, at the library's turn boundary: `Switched` wraps any `TurnAgent`, so
both edges (HTTP and the chat channel) and any agent get it the same way; which
store holds the key is the composition root's overlay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent_harness import context as ctx
from agent_harness import telemetry as tel
from agent_harness.config.settings import Key, Settings
from agent_harness.contracts import (
    CheckpointStore,
    Escalations,
    Identity,
    Refused,
    RunId,
    TurnResult,
    new_conversation_id,
    new_run_id,
)
from agent_harness.entrypoint import TurnAgent, ending
from agent_harness.entrypoint.persist import TurnPersister, delivered
from agent_harness.loop import Gone
from agent_harness.state import Conversation
from agent_harness.telemetry import counters

ENABLED = Key("agent.enabled", bool, True)
"""The switch: declared on the composition root's config port beside its own keys."""

PAUSED = (
    "The assistant is paused for now, so I cannot act on this message. It has been"
    " saved with your conversation and nothing has been changed. Please try again later."
)
"""What every turn is told while paused. Honest: it promises no person and no time."""

RULE = "agent.paused"


@dataclass
class Switched:
    """A `TurnAgent` that asks `agent.enabled` before every turn."""

    agent: TurnAgent
    settings: Settings
    reply: str = PAUSED
    """The agent's own words for the paused reply, when it has them."""
    history_chars: int = 32_000

    @property
    def store(self) -> CheckpointStore:
        return self.agent.store

    @property
    def escalations(self) -> Escalations | None:
        return self.agent.escalations

    def enabled(self) -> bool:
        return self.settings.get(ENABLED)

    async def opening(self, identity: Identity) -> str:
        return await self.agent.opening(identity) if self.enabled() else self.reply

    async def handle(
        self,
        text: str,
        *,
        identity: Identity,
        conversation: Conversation | None = None,
        run_id: RunId | None = None,
        delivery_id: str | None = None,
        gone: Gone | None = None,
    ) -> tuple[TurnResult, Conversation]:
        enabled = self.enabled()
        with ending.switched(enabled):
            if enabled:
                return await self.agent.handle(
                    text,
                    identity=identity,
                    conversation=conversation,
                    run_id=run_id,
                    delivery_id=delivery_id,
                    gone=gone,
                )
            return await self._paused(text, identity, conversation, run_id, delivery_id)

    async def _paused(
        self,
        text: str,
        identity: Identity,
        conversation: Conversation | None,
        run_id: RunId | None,
        delivery_id: str | None,
    ) -> tuple[TurnResult, Conversation]:
        conversation, run_id = delivered(identity.customer_id, conversation, run_id, delivery_id)
        run_id = run_id or new_run_id()
        conversation = conversation or Conversation(
            conversation_id=new_conversation_id(), customer_id=identity.customer_id
        )
        started = time.monotonic()
        result = Refused(reply=self.reply, reason="the agent is paused", rule_id=RULE)
        opened = ending.opened(run_id, conversation, identity, None, False)
        with tel.turn_scope(run_id, None), tel.span("agent.turn", **opened) as span:
            tel.set_payload(span, tel.INPUT, text)
            ending.closed(span, result, None, started, False)
            counters.turns.add(1, {"result": "paused", "synthetic": "false"})
            kept = conversation.with_messages(ctx.user_message(text)).recording(result)
            stored = await TurnPersister(self.store, self.history_chars).persist(run_id, kept)
        return result, stored


__all__ = ["ENABLED", "PAUSED", "RULE", "Switched"]
