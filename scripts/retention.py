"""Delete what the harness wrote more than the retention window ago (Q-RETENTION, T-072).

    AGENT_DATABASE_URL=postgresql://... uv run python scripts/retention.py
    uv run python scripts/retention.py --dsn postgresql:///support_agent --days 30

Run once a day by the deployment's scheduler — cron, a Kubernetes CronJob, a
systemd timer; whichever already runs its other jobs. It is not a Temporal
schedule on purpose: Temporal is behind the `durable` profile and a database
without it still owes retention. Idempotent, so a missed day is caught up by the
next run and two overlapping runs delete nothing twice.

Prints one line per store and exits non-zero on failure, so a scheduler that
alerts on a failed job alerts on this. What this does not reach — Prometheus,
Temporal, Langfuse — is bounded (or not) by their own configuration; see
`support_agent.erasure.retention`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from cryptography.fernet import Fernet
from psycopg_pool import AsyncConnectionPool

from support_agent.config import Settings
from support_agent.erasure import retention
from support_agent.requests.postgres import PostgresRequests
from support_agent.state.postgres import PostgresCheckpointStore, PostgresSessionStore


async def main(dsn: str, days: int, now: int) -> retention.Expired:
    pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
    await pool.open(wait=True, timeout=10)
    try:
        # Expiry never decrypts a token, so the key only has to be a key. A
        # deployment without a portal has no AGENT_SESSION_KEY and may still
        # hold rows from one that had.
        key = Settings().session_key.encode() or Fernet.generate_key()
        return await retention.expire(
            now=now,
            days=days,
            checkpoints=PostgresCheckpointStore(pool),
            requests=PostgresRequests(pool),
            sessions=PostgresSessionStore(pool, key=key),
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("AGENT_DATABASE_URL", ""))
    parser.add_argument("--days", type=int, default=Settings().retention_days)
    parser.add_argument(
        "--now", type=int, default=None, help="epoch seconds to count back from (default: now)"
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("no database: pass --dsn or set AGENT_DATABASE_URL")
    done = asyncio.run(
        main(args.dsn, args.days, int(time.time()) if args.now is None else args.now)
    )
    print(f"retention: {args.days} days, deleting what was written before {done.before}")
    print(f"  checkpoints  {done.runs}")
    print(f"  requests     {done.names}")
    print(f"  sessions     {done.sessions}")
