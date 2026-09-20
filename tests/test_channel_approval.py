"""A waiting approval reaches the inbox the colleague already works in (T-059).

Escalation has pushed to Chatwoot since T-026 and approvals never did, so an
approval sat in Temporal making no noise until its timer expired it. The push
is a **doorbell, never a verdict**: the note says what is waiting and where to
decide it, and nothing in the inbox can grant anything — a link that could
would be a link a mail scanner could click.

The agent is a stub here on purpose. What is under test is what the channel
does with a `NeedsApproval`, not how a turn comes to produce one, which
`test_approver` already covers against real workflows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest
from evals import issuer as issuing
from starlette.testclient import TestClient

from support_agent import channel as ch
from support_agent import telemetry as tel
from support_agent.contracts import Completed, NeedsApproval
from support_agent.identity.sessions import Resume, StoredSession
from support_agent.state import Conversation, InMemoryCheckpointStore, InMemorySessionStore

SECRET = "local-dev-only"
PORTAL = "http://portal.test"
MESSAGE = json.loads(
    (Path(__file__).parent / "fixtures" / "chatwoot" / "message_created.json").read_text()
)
SUBJECT = MESSAGE["sender"]["identifier"]  # login-C-1042, as Chatwoot sends it


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


class Recorder:
    """Chatwoot, as the channel calls it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    async def reply(self, account: int, conversation: int, text: str) -> None:
        self.calls.append(("reply", conversation, text))

    async def note(self, account: int, conversation: int, text: str) -> None:
        self.calls.append(("note", conversation, text))

    async def hand_off(self, account: int, conversation: int) -> None:
        self.calls.append(("hand_off", conversation, ""))

    async def open_conversation(self, account: int, source_id: str) -> tuple[int, bool]:
        self.calls.append(("open", 77, source_id))
        return 77, True


class Waiting:
    """An agent whose turn ends with a refund somebody has to authorise."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.store = InMemoryCheckpointStore()

    async def handle(self, text: str, **kwargs: Any) -> tuple[Any, Conversation]:
        conversation = kwargs.get("conversation") or Conversation(
            conversation_id="cnv_waiting", customer_id="C-1042"
        )
        return self.result, conversation


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()))
    mac = hmac.new(SECRET.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, {
        "content-type": "application/json",
        "x-chatwoot-timestamp": stamp,
        "x-chatwoot-signature": f"sha256={mac}",
    }


async def incoming(result: Any) -> list[tuple[str, int, str]]:
    """One customer message through the real receiver, and what Chatwoot was told."""
    api, sessions, grant = Recorder(), InMemorySessionStore(), issuing.LocalRefresh()
    token = grant.login("C-1042", subject=SUBJECT)
    await sessions.put(StoredSession(subject=SUBJECT, refresh_token=token, updated_at=0))

    agent = Waiting(result)
    channel = ch.Channel(
        agent=agent,
        store=agent.store,
        sessions=Resume(sessions, grant, issuer=issuing.issuer()),
        api=api,
        portal_url=PORTAL,
        secret=SECRET,
    )
    raw, headers = signed(MESSAGE)
    client = TestClient(ch.build(channel))
    answered = client.post("/webhook", content=raw, headers=headers)
    assert answered.status_code in (200, 202), answered.text
    return api.calls


WAITING = NeedsApproval(
    approval_id="apr_3ab82d2b33cc",
    action="issue_refund",
    reason="the order is refunded, and a refund that is not owed needs a person",
    reply="I have sent this to a colleague to authorise. Nothing has been refunded yet.",
)


@pytest.mark.discharges("AHC-0070", "AAC-0043")
async def test_a_waiting_approval_is_put_in_front_of_a_person() -> None:
    calls = await incoming(WAITING)

    assert [c[0] for c in calls] == ["reply", "note", "hand_off"]
    reply, note = calls[0][2], calls[1][2]
    assert "Nothing has been refunded yet" in reply, "the customer is promised nothing"
    assert "apr_3ab82d2b33cc" in note, "the reference a person decides by"
    assert "issue_refund" in note
    assert "not owed" in note, "why it needs a person, not just that it does"
    assert "/ops/desk" in note, "where to decide it"


@pytest.mark.discharges("AAC-0078")
async def test_the_note_is_a_doorbell_and_never_a_verdict() -> None:
    """Nothing in the inbox may decide anything. A note carrying a link that
    grants is a decision a mail scanner can make by fetching a URL."""
    note = (await incoming(WAITING))[1][2]

    assert "granted" not in note.lower()
    assert "approve" not in note.lower(), "no one-click grant, by construction"
    assert "token" not in note.lower() and "http" not in note.lower()


@pytest.mark.discharges("AHC-0070")
async def test_a_turn_that_needs_nobody_tells_nobody() -> None:
    """The quiet path matters as much: an ordinary answer must not appear in
    the desk's inbox, or the desk stops reading it."""
    calls = await incoming(Completed(reply="Your order is on its way."))

    assert [c[0] for c in calls] == ["reply"]
