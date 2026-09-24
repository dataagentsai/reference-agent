"""The durable store — P6.

The correctness is in the statements, not in the code around them. Each of the
three is a single round trip whose `WHERE` carries the rule, so two processes
racing on a retry cannot both win and neither has to hold a lock.

**No sweeper.** A claim whose holder was killed outright never settles, because
`finally` does not run for that. The expiry is therefore checked *when the next
claim is taken*, in the same statement that takes it — so an abandoned name is
reclaimed by the caller who wants it rather than by a job that goes looking.
That is what removed a Temporal workflow from this concern (T-062).
"""

from __future__ import annotations

import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from support_agent.requests import (
    CLAIM_TTL_S,
    AlreadyAnswered,
    Claim,
    Scope,
    StillRunning,
)


class PostgresRequests:
    durable = True

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def claim(self, name: str, *, scope: Scope, ttl_s: int = CLAIM_TTL_S) -> Claim:
        """Take the name, or say which refusal this is.

        One statement. The `WHERE` on the conflict branch is the whole guard: it
        matches only a claim that expired, so a live holder and an answered name
        both leave zero rows and the second query below says which. Two callers
        racing here both attempt the same insert and exactly one changes a row.
        """
        async with self._pool.connection() as conn:
            taken = await (
                await conn.execute(
                    """
                    INSERT INTO agent_state.requests (name, scope, state, expires_at)
                    VALUES (%s, %s, 'in_flight', now() + make_interval(secs => %s))
                    ON CONFLICT (name) DO UPDATE
                        SET scope = EXCLUDED.scope,
                            state = 'in_flight',
                            outcome = NULL,
                            expires_at = EXCLUDED.expires_at
                      WHERE agent_state.requests.state = 'in_flight'
                        AND agent_state.requests.expires_at < now()
                    RETURNING name
                    """,
                    (name, scope.value, ttl_s),
                )
            ).fetchone()
            if taken is not None:
                return Claim(name=name, scope=scope)

            row = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT state, outcome FROM agent_state.requests WHERE name = %s", (name,)
                )
            ).fetchone()

        if row is not None and row["state"] == "answered":
            raise AlreadyAnswered(f"{name!r} was already answered", outcome=row["outcome"])
        raise StillRunning(f"{name!r} is already being handled")

    async def settle(self, name: str, outcome: dict[str, object] | None = None) -> None:
        """A definite answer, recorded once.

        `state <> 'answered'` rather than a read followed by a write: the first
        definite answer for a name is the answer, and an application-level check
        would leave a window between the two where a second attempt could
        rewrite what the first one did.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_state.requests
                   SET state = 'answered', outcome = %s, expires_at = NULL
                 WHERE name = %s AND state <> 'answered'
                """,
                (None if outcome is None else Jsonb(json.loads(json.dumps(outcome))), name),
            )

    async def abandon(self, name: str) -> None:
        """No definite answer: the name goes back, so a retry may take it again
        and go out **under the same name**. Only an unanswered claim is dropped
        — an answered one is a record, not a lock."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM agent_state.requests WHERE name = %s AND state = 'in_flight'",
                (name,),
            )

    async def redact(self, runs: tuple[str, ...]) -> int:
        """Keep the name, drop the answer — F-056. Returns how many rows changed.

        This was a `DELETE` when the finding was written, and the finding's own
        open question is why it is not one now: removing an answered row makes
        that name executable again, so a replay arriving months later — a queue
        draining, a retry nobody remembered — would run a refund again for
        somebody who asked to be forgotten. The row is what refuses it.

        Keeping the row intact is the other wrong answer: the outcome is the
        far end's reply, and a reply about a person is about that person.

        So the name and `state = 'answered'` stay and the outcome goes. The
        guard was never the body; it is the name being taken.

        `split_part` rather than `LIKE 'run:%'`, which would also match a run
        whose id merely starts the same way. It is a sequential scan over a
        table nobody else scans, on a path that runs when a person asks — the
        cost is real and it is paid rarely, which is the right way round.
        """
        if not runs:
            return 0
        async with self._pool.connection() as conn:
            done = await conn.execute(
                """
                UPDATE agent_state.requests
                   SET outcome = NULL
                 WHERE split_part(name, ':', 1) = ANY(%s)
                   AND outcome IS NOT NULL
                """,
                (list(runs),),
            )
        return done.rowcount


__all__ = ["PostgresRequests"]
