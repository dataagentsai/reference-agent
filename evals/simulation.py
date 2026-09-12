"""This implementation, as a scenario sees it — the far side of the agent contract.

**Outside `src/` deliberately.** The import contract says *the agent cannot see
its simulator*, and this module is where the two meet — so it lives beside the
evaluation code that already imports both. Putting it in the package would have
let the agent reach its own twin, which is the one direction that must never
work: a system that can see its simulator can be written to pass it.

A declared scenario names a world, a customer and what it expects, and knows
nothing about how this agent is built. Everything it does not know lives here:
which provider, which stores, which scopes the projected world is gated with,
how an identity is minted from a customer id.

**That is the whole point of the split.** A regenerated agent will make different
choices at every line below, and the same scenario file has to drive it — so the
contract is three callables and this module is the only thing that changes.

    async with subject_for(live, llm=..., clock=...) as subject:
        record, outcomes = await run_file(path, subject=subject)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agenttwin import Approver, Desk, Live, Subject, project

from support_agent import approvals as ap
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import identity as ident
from support_agent.binding import SCOPES
from support_agent.contracts import Clock, Identity, LLMClient
from support_agent.idempotency import InMemoryLedger
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import connect

DECISIONS = {"grant": Approver.grants, "refuse": Approver.denies, "never": Approver.silent}
RESOLUTIONS = {"handled": Desk.answers, "never": Desk.never_comes}


@asynccontextmanager
async def subject_for(
    live: Live, *, llm: LLMClient, clock: Clock | None = None
) -> AsyncIterator[Subject]:
    """Wire this agent against a live world and hand back what a scenario drives."""
    approvals = ap.InMemoryApprovalStore()
    escalations = esc.InMemoryEscalationStore()

    async with connect(project(live, scopes=SCOPES), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            escalations=escalations,
            clock=clock,
        )

        async def say(
            text: str, customer_id: str, conversation: object
        ) -> tuple[str, Conversation]:
            """The customer speaks and reads a reply. Which typed result produced
            that reply is this implementation's business, not the scenario's."""
            who = Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)
            held = conversation if isinstance(conversation, Conversation) else None
            result, held = await agent.handle(text, identity=who, conversation=held)
            reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
            return reply, held

        def reviewer(decision: str, by: str) -> Approver:
            return DECISIONS[decision](approvals, ap.decide, name=by)

        def colleague(resolution: str, by: str) -> Desk:
            return RESOLUTIONS[resolution](escalations, esc.resolve, name=by)

        yield Subject(say=say, reviewer=reviewer, colleague=colleague)


__all__ = ["subject_for"]
