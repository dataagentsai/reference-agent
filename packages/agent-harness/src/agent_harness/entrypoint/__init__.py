"""The parts of a turn every agent's entrypoint uses, and what an edge drives.

An agent's turn — which routes it has, what each answers, what it asks before
acting — is its own orchestration (the reference agent's is
`support_agent.entrypoint`). What it is built from is here: handing a
conversation to a person (`handoff`), recording a turn as it opens and ends and
screening its reply (`ending`), and persisting what the turn changed (`persist`).
What the edges drive is `TurnAgent`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent_harness.contracts import (
        CheckpointStore,
        Escalations,
        Identity,
        RunId,
        TurnResult,
    )
    from agent_harness.loop import Gone
    from agent_harness.state import Conversation


@runtime_checkable
class TurnAgent(Protocol):
    """What an edge drives: one turn in, one typed result out, and the stores the
    edge must agree with. The HTTP edge and the chat channel take this rather
    than any one agent's class, so a second agent is served by the same edges.
    Checked at runtime by member, which is how `serve` tells a ready agent from
    a factory that builds one."""

    @property
    def store(self) -> CheckpointStore: ...
    @property
    def escalations(self) -> Escalations | None: ...

    async def opening(self, identity: Identity) -> str: ...

    async def handle(
        self,
        text: str,
        *,
        identity: Identity,
        conversation: Conversation | None = None,
        run_id: RunId | None = None,
        delivery_id: str | None = None,
        gone: Gone | None = None,
    ) -> tuple[TurnResult, Conversation]: ...


__all__ = ["TurnAgent"]
