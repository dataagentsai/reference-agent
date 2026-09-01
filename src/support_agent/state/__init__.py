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
        self._lock = asyncio.Lock()

    async def checkpoint(self, run_id: RunId, state: bytes) -> None:
        async with self._lock:
            self._runs[run_id] = state

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._lock:
            return self._runs.get(run_id)


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

    async def checkpoint(self, run_id: RunId, state: bytes) -> None:
        async with self._lock:
            target = self._path(run_id)
            with tempfile.NamedTemporaryFile(dir=self._dir, delete=False, suffix=".tmp") as handle:
                handle.write(state)
                temporary = Path(handle.name)
            temporary.replace(target)

    async def resume(self, run_id: RunId) -> bytes | None:
        async with self._lock:
            target = self._path(run_id)
            if not target.exists():
                return None
            raw = target.read_bytes()
        try:
            json.loads(raw)
        except ValueError:
            return None
        return raw


__all__ = ["Conversation", "FileCheckpointStore", "InMemoryCheckpointStore"]
