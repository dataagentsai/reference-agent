"""Writing a turn down — bounded, checkpointed, and handed back as stored.

One job: every checkpoint goes through here. Three call sites once wrote the
conversation and none capped it, so the one durable structure in the system
grew without limit on every turn.

It lives beside the entrypoint rather than in `state`, because bounding is a
`context` decision and `state` sits below `context` in the import contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness import context as ctx
from agent_harness import telemetry as tel
from agent_harness.contracts import CheckpointStore, RunId
from agent_harness.state import Conversation


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
        await self.store.checkpoint(
            run_id,
            raw,
            conversation_id=bounded.conversation_id,
            # Whose, from the conversation rather than from the caller's
            # identity — F-056. The conversation is what the turn was
            # recorded against, and an identity can be elevated.
            customer_id=bounded.customer_id,
        )
        return bounded


def agree_on_durability(**stores: object) -> None:
    """Refuse a set of stores that disagree about surviving a restart.

    The conversation holds **pointers** — `pending_approval_id` and
    `pending_escalation_id` — so a durable folder beside a volatile approval
    store is worse than losing both. After a restart the folder comes back
    saying a colleague has the customer's refund, the approval it names is
    gone, and `ApprovalFlow.resume` reads that absence as *nothing to resume*
    and answers the next question as though nothing were owed. No error, no log
    line, no refund. The one thing a conversation cannot survive is outliving
    what it points at.

    Every store has declared `durable` since it was written and nothing has
    ever read it. This is the read. It is the same check `serve.build` makes
    about *which* store the desk was given, in the other dimension: one agent,
    one queue, one answer about what a restart costs.
    """
    stated = {n: getattr(s, "durable", None) for n, s in stores.items() if s is not None}
    known = {n: d for n, d in stated.items() if d is not None}
    if len(set(known.values())) > 1:
        kept = ", ".join(sorted(n for n, d in known.items() if d))
        lost = ", ".join(sorted(n for n, d in known.items() if not d))
        raise ValueError(
            f"durable {kept} wired beside volatile {lost} — a conversation that outlives "
            "what it points at loses the pointer silently. Pick one row: everything in "
            "memory, or everything durable."
        )


__all__ = ["TurnPersister", "agree_on_durability"]
