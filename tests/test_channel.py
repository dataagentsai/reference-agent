"""T-026 (C): Chatwoot's webhook, and what it may cause.

Every row starts from `fixtures/chatwoot/message_created.json`, a payload
captured from Chatwoot v4.17.1 on 16 September when a verified contact wrote to
the bot, so the shape tested is the shape Chatwoot sends. Each row changes one
thing and states what the agent must do: act and reply, ask the customer to
sign in, or nothing at all. Chatwoot's side is a recorder; the agent, the shop
and the stored logins are real.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from agenttwin import Live, load, project
from evals import issuer as issuing
from starlette.testclient import TestClient

from support_agent import channel as ch
from support_agent import entrypoint as ep
from support_agent import escalation as esc
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.contracts import StoredSession
from support_agent.idempotency import InMemoryLedger
from support_agent.identity.sessions import Resume
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore, InMemorySessionStore
from support_agent.tools import connect

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "chatwoot" / "message_created.json").read_text()
)
WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
SECRET = "bot-webhook-secret"
PORTAL = "http://localhost:8077/portal/"
SUBJECT = FIXTURE["sender"]["identifier"]  # login-C-1042


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


class Recorder:
    """Chatwoot's application API, as far as the bot uses it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    async def reply(self, account: int, conversation: int, text: str) -> None:
        self.calls.append(("reply", conversation, text))

    async def note(self, account: int, conversation: int, text: str) -> None:
        self.calls.append(("note", conversation, text))

    async def hand_off(self, account: int, conversation: int) -> None:
        self.calls.append(("hand_off", conversation, ""))


def signed(body: dict, *, secret: str = SECRET, at: float | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    stamp = str(int(at if at is not None else time.time()))
    mac = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "content-type": "application/json",
        "x-chatwoot-timestamp": stamp,
        "x-chatwoot-signature": f"sha256={mac}",
    }


async def post(body: dict, *, logged_in: bool = True, headers=None, twice: bool = False):
    """One webhook through the real receiver, and what Chatwoot was told."""
    api, sessions, grant = Recorder(), InMemorySessionStore(), issuing.LocalRefresh()
    if logged_in:
        token = grant.login("C-1042", subject=SUBJECT)
        await sessions.put(StoredSession(subject=SUBJECT, refresh_token=token, updated_at=0))
    store = InMemoryCheckpointStore()
    world = Live.start(load(WORLD))
    async with connect(project(world), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=ScriptedClient([]),
            tools=tools,
            store=store,
            escalations=esc.InMemoryEscalationStore(),
            deliveries=trg.InMemoryDeliveryLog(),
        )
        channel = ch.Channel(
            agent=agent,
            store=store,
            sessions=Resume(sessions, grant, issuer=issuing.issuer()),
            api=api,
            portal_url=PORTAL,
            secret=SECRET,
        )
        client = TestClient(ch.build(channel))
        raw, signature = signed(body)
        sent = {**signature, **(headers or {})}
        responses = [client.post("/webhook", content=raw, headers=sent)]
        if twice:
            responses.append(client.post("/webhook", content=raw, headers=sent))
    return responses, api.calls


def changed(**edits) -> dict:
    body = copy.deepcopy(FIXTURE)
    for path, value in edits.items():
        target = body
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return body


# (why, the webhook, logged in, status, what Chatwoot is told)
ROWS = [
    ("a verified, logged-in customer asks", changed(), True, 202, ["reply:AB-10003"]),
    ("the bot's own reply comes back", changed(message_type="outgoing"), True, 200, []),
    ("a private note", changed(private=True), True, 200, []),
    ("a person has the conversation", changed(conversation__status="open"), True, 200, []),
    (
        "an anonymous visitor",
        changed(conversation__meta__hmac_verified=False),
        True,
        202,
        ["reply:sign in"],
    ),
    ("a verified contact whose login has ended", changed(), False, 202, ["reply:sign in again"]),
]


@pytest.mark.parametrize(
    ("why", "body", "logged_in", "status", "told"), ROWS, ids=[r[0] for r in ROWS]
)
@pytest.mark.discharges("AHC-0099", "AAC-0111", "P-OWNERSHIP")
async def test_a_webhook_causes_only_what_it_may(
    why: str, body: dict, logged_in: bool, status: int, told: list[str]
) -> None:
    (response,), calls = await post(body, logged_in=logged_in)
    assert response.status_code == status
    assert [c[0] for c in calls] == [t.split(":")[0] for t in told]
    for (_, _, text), expected in zip(calls, told, strict=True):
        assert expected.split(":", 1)[1].lower() in text.lower(), text


# (why, what is wrong with the signature)
UNSIGNED = [
    ("no signature", {"x-chatwoot-signature": ""}),
    ("signed with another secret", "other"),
    ("replayed after ten minutes", "stale"),
]


@pytest.mark.parametrize(("why", "wrong"), UNSIGNED, ids=[u[0] for u in UNSIGNED])
@pytest.mark.discharges("AAC-0111")
async def test_an_unsigned_webhook_causes_nothing(why: str, wrong) -> None:
    if wrong == "other":
        headers = signed(FIXTURE, secret="not-the-bots-secret")[1]
    elif wrong == "stale":
        headers = signed(FIXTURE, at=time.time() - 600)[1]
    else:
        headers = wrong
    (response,), calls = await post(FIXTURE, headers=headers)
    assert (response.status_code, calls) == (401, [])


@pytest.mark.discharges("AAC-0076")
async def test_a_redelivered_message_is_answered_once() -> None:
    responses, calls = await post(FIXTURE, twice=True)
    assert [r.status_code for r in responses] == [202, 202]
    assert [c[0] for c in calls] == ["reply"]


@pytest.mark.discharges("AHC-0070", "AAC-0110")
async def test_an_escalation_hands_the_conversation_to_a_person() -> None:
    (response,), calls = await post(changed(content="put me through to a human"))

    assert response.status_code == 202
    assert [c[0] for c in calls] == ["reply", "note", "hand_off"]
    assert "E-" in calls[0][2], "the customer is told the reference"
    assert "put me through to a human" in calls[1][2], "the person is handed what was asked"
