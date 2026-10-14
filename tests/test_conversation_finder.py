"""`ConversationFinder.newest_for`: a customer's newest conversation, found by
who they are rather than by an id only their browser held (claims-fnol-azure
F-117: a reload or a closed tab started a new conversation).

One table, every store that keeps conversations. PostgreSQL's cases are in
test_postgres.py, beside its other durable-store tests, because its write time
is the database's `now()` and cannot be moved by a test clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.contracts import CheckpointStore, ConversationFinder, ConversationId, RunId
from agent_harness.state import FileCheckpointStore, InMemoryCheckpointStore


class Clock:
    def __init__(self, at: int = 1_000) -> None:
        self.at = at

    def __call__(self) -> int:
        return self.at


def make(kind: str, clock: Clock, tmp_path: Path) -> CheckpointStore:
    if kind == "memory":
        return InMemoryCheckpointStore(clock=clock)
    return FileCheckpointStore(tmp_path, clock=clock)


# (why, writes as (at, run, conversation, customer, state), asked for, expected (state, at) or None)
CASES = [
    ("nothing written finds nothing", [], "PH-1001", None),
    (
        "one conversation is found, with its write time",
        [(1_000, "run_a", "cnv_1", "PH-1001", b'{"n":1}')],
        "PH-1001",
        (b'{"n":1}', 1_000),
    ),
    (
        "the newest of two conversations wins",
        [
            (1_000, "run_a", "cnv_1", "PH-1001", b'{"n":1}'),
            (2_000, "run_b", "cnv_2", "PH-1001", b'{"n":2}'),
        ],
        "PH-1001",
        (b'{"n":2}', 2_000),
    ),
    (
        "a later turn of an older conversation makes it the newest again",
        [
            (1_000, "run_a", "cnv_1", "PH-1001", b'{"n":1}'),
            (2_000, "run_b", "cnv_2", "PH-1001", b'{"n":2}'),
            (3_000, "run_c", "cnv_1", "PH-1001", b'{"n":3}'),
        ],
        "PH-1001",
        (b'{"n":3}', 3_000),
    ),
    (
        "another customer's conversation is never found",
        [(1_000, "run_a", "cnv_1", "PH-1002", b'{"n":1}')],
        "PH-1001",
        None,
    ),
    (
        "an empty customer id finds nothing, even with unattributed turns",
        [(1_000, "run_a", "cnv_1", "", b'{"n":1}')],
        "",
        None,
    ),
]


@pytest.mark.parametrize("kind", ["memory", "file"])
@pytest.mark.parametrize(("why", "writes", "asked", "expected"), CASES, ids=[c[0] for c in CASES])
async def test_newest_for(
    kind: str, why: str, writes, asked: str, expected, tmp_path: Path
) -> None:
    clock = Clock()
    store = make(kind, clock, tmp_path)
    assert isinstance(store, ConversationFinder)
    for at, run, conversation, customer, state in writes:
        clock.at = at
        await store.checkpoint(
            RunId(run), state, conversation_id=ConversationId(conversation), customer_id=customer
        )
    assert await store.newest_for(asked) == expected, why
