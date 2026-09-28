"""Checkpoints in a directory — durable without infrastructure.

Split from `state` so that module stays under the size ratchet once retention
(Q-RETENTION) gave every store a second way to lose rows. Nothing about the
store changed in the move; `state` re-exports it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from support_agent.contracts import Clock, ConversationId, RunId


def _wall() -> int:
    """The default clock, and the only place this module reads the wall."""
    return int(time.time())


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

    def __init__(self, directory: Path | str, *, clock: Clock | None = None) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._clock: Clock = clock or _wall
        """Stamps every file it writes (Q-RETENTION). The file's own mtime
        would be the filesystem's clock, which no test can move."""

    def _path(self, run_id: RunId) -> Path:
        return self._dir / f"{run_id}.json"

    def _conversation_path(self, conversation_id: ConversationId) -> Path:
        return self._dir / f"conversation-{conversation_id}.json"

    async def checkpoint(
        self,
        run_id: RunId,
        state: bytes,
        *,
        conversation_id: ConversationId,
        customer_id: str = "",
    ) -> None:
        async with self._lock:
            self._write(self._path(run_id), state)
            if customer_id:
                # A third file, holding only which run and which conversation
                # belong to whom — F-056. A directory cannot be queried, so
                # erasure either reads and decodes every file or something
                # writes down the one fact it needs. This is that fact, and it
                # is small enough that reading all of them is cheap.
                self._write(
                    self._dir / f"whose-{run_id}.json",
                    json.dumps(
                        {
                            "customer_id": customer_id,
                            "run_id": run_id,
                            "conversation_id": conversation_id,
                        }
                    ).encode(),
                )
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
        written = self._clock()
        os.utime(temporary, (written, written))
        temporary.replace(target)

    async def resume(self, run_id: RunId) -> bytes | None:
        return await self._read(self._path(run_id))

    async def latest(self, conversation_id: ConversationId) -> bytes | None:
        return await self._read(self._conversation_path(conversation_id))

    async def forget(self, customer_id: str) -> tuple[RunId, ...]:
        """Every file this customer's turns produced, removed — F-056.

        The index files go last. Each one is what makes its own run findable,
        so removing it before the state it points at would strand that state
        where nothing could ever ask for it again — which looks like erasure
        and is the opposite.
        """
        if not customer_id:
            return ()
        async with self._lock:
            runs: list[RunId] = []
            for index in sorted(self._dir.glob("whose-*.json")):
                try:
                    whose = json.loads(index.read_bytes())
                except (OSError, ValueError):  # pragma: no cover - a torn write
                    continue
                if whose.get("customer_id") != customer_id:
                    continue
                self._path(RunId(whose["run_id"])).unlink(missing_ok=True)
                self._conversation_path(whose["conversation_id"]).unlink(missing_ok=True)
                index.unlink(missing_ok=True)
                runs.append(RunId(whose["run_id"]))
            return tuple(runs)

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

    async def expire(self, before: int) -> int:
        """Every file last written before `before`, removed — Q-RETENTION.

        Each file is judged by its own stamp, so a conversation pointer that a
        turn rewrote yesterday stays while that conversation's month-old runs
        go — the same outcome as Postgres, where `latest` reads the newest row
        that remains. Index files go last, for `forget`'s reason: an index
        removed before its state strands that state where nothing can find it.
        """
        async with self._lock:
            aged = [p for p in self._dir.glob("*.json") if p.stat().st_mtime < before]
            aged.sort(key=lambda p: p.name.startswith("whose-"))
            for path in aged:
                path.unlink(missing_ok=True)
            return sum(1 for p in aged if not p.name.startswith(("whose-", "conversation-")))


__all__ = ["FileCheckpointStore"]
