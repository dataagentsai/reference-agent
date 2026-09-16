"""T-026 (A): a customer's login, kept server-side, turned back into a session.

A chat channel's webhook names a contact and carries no credential. The portal
keeps the customer's refresh token; these tests hold the two properties the
decision rests on: the agent acts only while that login is live, and a stored
token can only ever become the login it was stored for.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from cryptography.fernet import Fernet
from evals import issuer as issuing

from support_agent import identity as ident
from support_agent.contracts import StoredSession
from support_agent.identity import sessions
from support_agent.state import InMemorySessionStore

psycopg_pool = pytest.importorskip("psycopg_pool")

DSN = os.environ.get("AGENT_DATABASE_URL", "postgresql:///support_agent")
KEY = Fernet.generate_key()
T0 = 1_000_000


async def _postgres():
    from support_agent.state.postgres import PostgresSessionStore

    pool = psycopg_pool.AsyncConnectionPool(DSN, min_size=1, max_size=2, open=False)
    try:
        await pool.open(wait=True, timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database at {DSN}: {exc}")
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE agent_state.sessions")
    return pool, PostgresSessionStore(pool, key=KEY)


@pytest.fixture(params=["in-memory", "postgres"])
async def store(request):
    if request.param == "in-memory":
        yield InMemorySessionStore()
        return
    pool, durable = await _postgres()
    yield durable
    await pool.close()


@pytest.mark.discharges("AHC-0099")
async def test_a_stored_login_round_trips_and_logout_removes_it(store) -> None:
    await store.put(StoredSession(subject="login-7", refresh_token="rt-secret", updated_at=T0))
    assert (await store.get("login-7")).refresh_token == "rt-secret"
    await store.delete("login-7")
    assert await store.get("login-7") is None


@pytest.mark.discharges("AHC-0099")
async def test_a_refresh_token_is_ciphertext_at_rest() -> None:
    from support_agent.state.postgres import PostgresSessionStore, UnreadableSession

    pool, durable = await _postgres()
    try:
        await durable.put(
            StoredSession(subject="login-7", refresh_token="rt-secret", updated_at=T0)
        )
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT refresh_token FROM agent_state.sessions")
            (raw,) = await cur.fetchone()
        assert b"rt-secret" not in bytes(raw)
        with pytest.raises(UnreadableSession):
            await PostgresSessionStore(pool, key=Fernet.generate_key()).get("login-7")
    finally:
        await pool.close()


def resume(store, grant) -> sessions.Resume:
    return sessions.Resume(store, grant, issuer=issuing.issuer(), clock=lambda: T0)


# (why, how the login is set up, the customer it must become or the failure)
CASES = [
    ("a live login becomes its customer", "live", "C-1042"),
    ("never logged in", "absent", ident.SessionEnded),
    ("logged out at the issuer", "revoked", ident.SessionEnded),
    ("a stored token that belongs to another login", "swapped", ident.SessionEnded),
    ("a staff login is no customer", "staff", ident.NotACustomer),
]


@pytest.mark.parametrize(("why", "setup", "expect"), CASES, ids=[c[0] for c in CASES])
@pytest.mark.discharges("AHC-0099", "AAC-0057", "P-OWNERSHIP")
async def test_a_stored_login_becomes_only_its_own_live_session(
    why: str, setup: str, expect
) -> None:
    store, grant = InMemorySessionStore(), issuing.LocalRefresh()
    subject = "login-C-1042"
    if setup in ("live", "revoked"):
        token = grant.login("C-1042", subject=subject)
        await store.put(StoredSession(subject=subject, refresh_token=token, updated_at=T0))
        if setup == "revoked":
            grant.revoke(token)
    elif setup == "swapped":
        other = grant.login("C-9999", subject="login-C-9999")
        await store.put(StoredSession(subject=subject, refresh_token=other, updated_at=T0))
    elif setup == "staff":
        token = grant.login(None, subject=subject)
        await store.put(StoredSession(subject=subject, refresh_token=token, updated_at=T0))

    if isinstance(expect, str):
        who = await resume(store, grant).identity_for(subject)
        assert (who.customer_id, who.subject) == (expect, subject)
        return
    with pytest.raises(expect):
        await resume(store, grant).identity_for(subject)
    if expect is ident.SessionEnded and setup != "absent":
        assert await store.get(subject) is None, "an ended login is not kept to be tried again"


@pytest.mark.discharges("AHC-0021")
async def test_a_burst_of_messages_is_one_refresh() -> None:
    store, grant = InMemorySessionStore(), issuing.LocalRefresh()
    token = grant.login("C-1042", subject="login-C-1042")
    await store.put(StoredSession(subject="login-C-1042", refresh_token=token, updated_at=T0))
    resumed = resume(store, grant)

    who = await asyncio.gather(*(resumed.identity_for("login-C-1042") for _ in range(5)))

    assert {w.customer_id for w in who} == {"C-1042"}
    assert grant.refreshes == 1


@pytest.mark.discharges("AHC-0099")
async def test_logout_reaches_the_agent_on_the_next_message() -> None:
    store, grant = InMemorySessionStore(), issuing.LocalRefresh()
    token = grant.login("C-1042", subject="login-C-1042")
    await store.put(StoredSession(subject="login-C-1042", refresh_token=token, updated_at=T0))
    resumed = resume(store, grant)
    await resumed.identity_for("login-C-1042")

    grant.revoke(token)
    await store.delete("login-C-1042")
    resumed.forget("login-C-1042")

    with pytest.raises(ident.SessionEnded):
        await resumed.identity_for("login-C-1042")
