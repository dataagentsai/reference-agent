"""What is remembered.

L5 · P6. Conversation, checkpoints, and the durability boundary between what
survives a crash and what does not.

**This is agent-owned state, and AgentTwin never projects it.** The business world
is simulated; conversation, checkpoints and approvals are the *oracle*. An oracle
that can be faked is not one — which is why this lives in its own schema with its
own credentials, and why `state` and the tool server never share a connection.

The durability boundary is stated per implementation rather than assumed.
`InMemoryCheckpointStore` says it does not survive a restart; `FileCheckpointStore`
says it does. P6 exists precisely because enforcement that dies with the process
is enforcement that is absent at the moment it was needed — an agent interrupted
mid-approval is the case, and it is not hypothetical.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from support_agent.contracts import ConversationId, Message, RunId


class Conversation(BaseModel):
    """What survives between turns.

    Deliberately not the whole trace: spans are for explaining a past run, this
    is for continuing one. Keeping them apart is what stops a checkpoint growing
    without bound.
    """

    model_config = ConfigDict(frozen=True)

    conversation_id: ConversationId
    customer_id: str
    messages: tuple[Message, ...] = ()
    pending_approval_id: str | None = None
    """Set when a turn ended in `NeedsApproval`. The next turn resumes from here
    rather than starting again — which is what "the approval returns, it does not
    block" means in storage terms."""
    pending_escalation_id: str | None = None
    """Set when a turn ended in `Escalated`. A person owns the conversation.

    Without this the escalation was not sticky: the agent said a colleague would
    take over and then answered the customer's next message itself, because
    nothing in what survives a turn recorded that the handoff had happened. One
    field, and it is the difference between a promise and a state.
    """

    def with_messages(self, *added: Message) -> Conversation:
        return self.model_copy(update={"messages": (*self.messages, *added)})

    def encode(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, raw: bytes) -> Conversation:
        return cls.model_validate_json(raw)


class InMemoryCheckpointStore:
    """Process-local. Correct within one run, gone at restart.

    Fine for tests and for a sealed simulation, where the process outlives the
    scenario by construction. Not fine where an approval may take a human an hour.
    """

    durable = False

    def __init__(self) -> None:
        self._runs: dict[str, bytes] = {}
        self._conversations: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def checkpoint(
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None:
        async with self._lock:
            self._runs[run_id] = state
            # The same bytes under both keys — F-006. The run index explains a
            # past turn; the conversation index is the only one a caller can
            # reach, because a customer holds a conversation id and never a run
            # id. One write, because two writes can disagree.
            self._conversations[conversation_id] = state

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        async with self._lock:
            return self._conversations.get(conversation_id)


class FileCheckpointStore:
    """Durable without infrastructure. Survives a restart; does not survive
    concurrent writers on different machines.

    Writes are atomic — a temporary file in the same directory, then `replace`,
    which is atomic on POSIX. A checkpoint half-written during a crash is worse
    than no checkpoint at all, because it resumes into a state that never
    existed.

    Postgres replaces this when there is more than one process, not before.
    """

    durable = True

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, run_id: RunId) -> Path:
        return self._dir / f"{run_id}.json"

    def _conversation_path(self, conversation_id: ConversationId) -> Path:
        return self._dir / f"conversation-{conversation_id}.json"

    async def checkpoint(
        self, run_id: RunId, state: bytes, *, conversation_id: ConversationId
    ) -> None:
        async with self._lock:
            self._write(self._path(run_id), state)
            # Written second and separately rather than symlinked or indexed:
            # both are atomic replaces, so a crash between them leaves the run
            # record correct and the conversation pointer one turn stale, which
            # is recoverable. An index that could point at a half-written file
            # would not be.
            self._write(self._conversation_path(conversation_id), state)

    def _write(self, target: Path, state: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=self._dir, delete=False, suffix=".tmp") as handle:
            handle.write(state)
            temporary = Path(handle.name)
        temporary.replace(target)

    async def resume(self, run_id: RunId) -> bytes | None:
        return await self._read(self._path(run_id))

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        return await self._read(self._conversation_path(conversation_id))

    async def _read(self, target: Path) -> bytes | None:
        async with self._lock:
            if not target.exists():
                return None
            raw = target.read_bytes()
        try:
            json.loads(raw)
        except ValueError:
            return None
        return raw


__all__ = ["Conversation", "FileCheckpointStore", "InMemoryCheckpointStore"]
