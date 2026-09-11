"""Writing a turn down — bounded, checkpointed, and handed back as stored.

One job: every checkpoint goes through here. Three call sites once wrote the
conversation and none capped it, so the one durable structure in the system
grew without limit on every turn.

It lives beside the entrypoint rather than in `state`, because bounding is a
`context` decision and `state` sits below `context` in the import contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from support_agent import context as ctx
from support_agent import telemetry as tel
from support_agent.contracts import CheckpointStore, RunId
from support_agent.state import Conversation


@dataclass(frozen=True)
class TurnPersister:
    store: CheckpointStore
    history_chars: int
    """Counted in characters rather than tokens because R-004 recorded that
    accurate counting is lost to the provider choice. The ruler is approximate;
    the bound is not."""

    async def persist(self, run_id: RunId, conversation: Conversation) -> Conversation:
        """Bound the history, write it, and hand back what was actually stored.

        The bounded copy is **returned**, not just written. A caller holding a
        larger history than the store does would be looking at state that no
        longer exists anywhere.
        """
        bounded = conversation.model_copy(
            update={"messages": ctx.bounded(conversation.messages, max_chars=self.history_chars)}
        )
        raw = bounded.encode()
        tel.set_current_attribute(tel.CONTEXT_STORED, len(raw))
        await self.store.checkpoint(run_id, raw, conversation_id=bounded.conversation_id)
        return bounded


__all__ = ["TurnPersister"]
