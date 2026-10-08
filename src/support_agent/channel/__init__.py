"""Chatwoot's agent-bot webhook: a customer's message in, a turn, a reply out (T-026).

Chatwoot runs the chat: the widget, the transcript, the desk where a person
takes a conversation over. This module is the agent's side of that, and it
decides what the webhook may cause, from four things checked in order:

1. **The signature.** `X-Chatwoot-Signature` is an HMAC of `timestamp.body`
   under the bot's secret; a timestamp more than five minutes off is refused
   too, so a captured webhook cannot be replayed later.
2. **Whether it is the customer's turn.** Only an incoming, public message in a
   conversation the bot still holds (`pending`) is acted on. The bot's own
   replies, Chatwoot's automated messages and anything in a conversation a
   person has taken are acknowledged and ignored.
3. **Who it is.** The contact must be `hmac_verified`: its identifier is a
   login's `sub`, and only the portal can compute its HMAC. An anonymous visitor
   is told to sign in.
4. **Whether that login is live.** The portal's stored login is resumed into a
   customer session (`identity.sessions.Resume`). Logged out, or a staff login,
   and the customer is told to sign in again. Nothing is done on their behalf.

**Opening the widget** is its own event (`webwidget_triggered`). For a contact
with no conversation yet, a login's identifier and a live login, the bot opens
the conversation, confirms Chatwoot marks it HMAC-verified, and posts what
`Agent.opening` shows: the customer's orders and work in flight, with no model
call (P-OPEN, T-001). Anyone else opening the widget is left alone; a greeting
is not worth a conversation on the desk for every anonymous visitor.

The turn runs after the webhook is answered, because a model turn can outlast
Chatwoot's webhook timeout. The Chatwoot message id is the delivery id, so a
redelivered webhook is recognised by the same guard `/chat` uses (AAC-0076). On
escalation the reply goes out, the handoff facts go in as a private note, and
the conversation is handed to a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness import telemetry as tel
from support_agent import identity as ident
from support_agent import requests as req
from support_agent.approvals import notify as ap_notify
from support_agent.contracts import (
    Approval,
    CheckpointStore,
    ConversationId,
    Escalated,
    Identity,
    NeedsApproval,
)
from support_agent.entrypoint import Agent
from support_agent.state import Conversation

MAX_SKEW_S = 300

SIGN_IN = "Please sign in so I can look at your orders: {portal}"
SIGNED_OUT = "Your session has ended. Please sign in again so I can help: {portal}"


class ChatwootApi(Protocol):
    """What the agent says back through Chatwoot, as its bot."""

    async def reply(self, account: int, conversation: int, text: str) -> None: ...

    async def note(self, account: int, conversation: int, text: str) -> None: ...

    async def hand_off(self, account: int, conversation: int) -> None: ...

    async def open_conversation(
        self, account: int, inbox: int, contact: int, source_id: str
    ) -> tuple[int, bool]:
        """A new conversation for a contact: its id, and whether Chatwoot marks
        the contact HMAC-verified."""
        ...


class ChatwootClient:
    """Chatwoot's application API with the bot's access token."""

    def __init__(self, base_url: str, *, token: str) -> None:
        self._base, self._token = base_url.rstrip("/"), token

    async def reply(self, account: int, conversation: int, text: str) -> None:
        await self._post(
            account, conversation, "messages", {"content": text, "message_type": "outgoing"}
        )

    async def note(self, account: int, conversation: int, text: str) -> None:
        body = {"content": text, "message_type": "outgoing", "private": True}
        await self._post(account, conversation, "messages", body)

    async def hand_off(self, account: int, conversation: int) -> None:
        await self._post(account, conversation, "toggle_status", {"status": "open"})

    async def open_conversation(
        self, account: int, inbox: int, contact: int, source_id: str
    ) -> tuple[int, bool]:
        body = {"source_id": source_id, "inbox_id": inbox, "contact_id": contact}
        created = await self._send(f"{self._base}/api/v1/accounts/{account}/conversations", body)
        meta = created.get("meta") or {}
        return int(created["id"]), bool(meta.get("hmac_verified"))

    async def _post(self, account: int, conversation: int, path: str, body: dict[str, Any]) -> None:
        url = f"{self._base}/api/v1/accounts/{account}/conversations/{conversation}/{path}"
        await self._send(url, body)

    async def _send(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "api_access_token": self._token},
        )

        def post() -> dict[str, Any]:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                parsed: dict[str, Any] = json.loads(response.read() or b"{}")
                return parsed

        return await asyncio.to_thread(post)


class Sessions(Protocol):
    async def identity_for(self, subject: str) -> Identity: ...


@dataclass(frozen=True)
class Incoming:
    """The parts of a webhook the agent acts on, read once."""

    account: int
    conversation: int
    message: int
    subject: str
    verified: bool
    text: str


@dataclass(frozen=True)
class Opening:
    """A contact opening the widget with no conversation yet."""

    account: int
    inbox: int
    contact: int
    source_id: str
    subject: str


@dataclass(frozen=True)
class Channel:
    agent: Agent
    store: CheckpointStore
    sessions: Sessions
    api: ChatwootApi
    portal_url: str
    secret: str = field(repr=False)
    clock: Callable[[], float] = time.time

    def signed(self, headers: Any, raw: bytes) -> bool:
        stamp, signature = headers.get("x-chatwoot-timestamp"), headers.get("x-chatwoot-signature")
        if not stamp or not signature or not stamp.isdigit():
            return False
        if abs(self.clock() - int(stamp)) > MAX_SKEW_S:
            return False
        expected = hmac.new(self.secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256)
        return hmac.compare_digest(signature, f"sha256={expected.hexdigest()}")


MAX_TEXT = 8 * 1024  # characters: the bound /chat puts on its whole body
TOO_LONG = "That message is too long for me. Could you send a shorter one?"


def incoming(payload: Any) -> Incoming | None:
    """A customer's message the bot should answer, or `None` for anything else."""
    if not isinstance(payload, dict) or payload.get("event") != "message_created":
        return None
    if payload.get("message_type") != "incoming" or payload.get("private"):
        return None
    conversation = payload.get("conversation") or {}
    if conversation.get("status") != "pending":
        return None  # a person has it
    text = payload.get("content")
    sender = payload.get("sender") or {}
    if not isinstance(text, str) or not text.strip():
        return None
    meta = conversation.get("meta") or {}
    return Incoming(
        account=int((payload.get("account") or {}).get("id", 0)),
        conversation=int(conversation.get("id", 0)),
        message=int(payload.get("id", 0)),
        subject=str(sender.get("identifier") or ""),
        verified=bool(meta.get("hmac_verified")) and bool(sender.get("identifier")),
        text=text.strip(),
    )


def opened(payload: Any) -> Opening | None:
    """A signed-in contact opening the widget afresh, or `None` for anything else."""
    if not isinstance(payload, dict) or payload.get("event") != "webwidget_triggered":
        return None
    if payload.get("current_conversation"):
        return None  # already talking; a second greeting is noise
    contact = payload.get("contact") or {}
    subject, source_id = contact.get("identifier"), payload.get("source_id")
    if not subject or not source_id:
        return None
    return Opening(
        account=int((payload.get("account") or {}).get("id", 0)),
        inbox=int((payload.get("inbox") or {}).get("id", 0)),
        contact=int(contact.get("id", 0)),
        source_id=str(source_id),
        subject=str(subject),
    )


async def webhook(request: Request) -> Response:
    channel: Channel = request.app.state.channel
    raw = await request.body()
    with tel.span("agent.channel.webhook") as span:
        if not channel.signed(request.headers, raw):
            span.set_attribute("agent.channel.outcome", "unsigned")
            return JSONResponse({"error": "not signed"}, status_code=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        message, opening = incoming(payload), opened(payload)
        if message is None and opening is None:
            span.set_attribute("agent.channel.outcome", "ignored")
            return JSONResponse({"status": "ignored"})
        span.set_attribute("agent.channel.outcome", "accepted")
    task = (
        BackgroundTask(answer, channel, message)
        if message is not None
        else BackgroundTask(greet, channel, opening)
    )
    return JSONResponse({"status": "accepted"}, status_code=202, background=task)


async def greet(channel: Channel, opening: Opening | None) -> None:
    """The conversation opened and the customer shown their orders (P-OPEN)."""
    if opening is None:
        return
    with tel.span("agent.channel.opening") as span:
        try:
            who = await channel.sessions.identity_for(opening.subject)
        except (ident.SessionEnded, ident.NotACustomer):
            span.set_attribute("agent.channel.outcome", "not-signed-in")
            return
        conversation, verified = await channel.api.open_conversation(
            opening.account, opening.inbox, opening.contact, opening.source_id
        )
        if not verified:
            span.set_attribute("agent.channel.outcome", "unverified")
            return
        await channel.api.reply(opening.account, conversation, await channel.agent.opening(who))
        span.set_attribute("agent.channel.outcome", "greeted")


async def answer(channel: Channel, message: Incoming) -> None:
    """The turn, after Chatwoot has its 202."""
    api, where = channel.api, (message.account, message.conversation)
    with tel.span("agent.channel.turn") as span:
        who = await _customer(channel, message)
        if isinstance(who, str):
            outcome = "too-long" if who is TOO_LONG else "sign-in"
            span.set_attribute("agent.channel.outcome", outcome)
            await api.reply(*where, who.format(portal=channel.portal_url))
            return
        conversation = await _conversation(channel.store, message, who)
        if conversation is None:
            span.set_attribute("agent.channel.outcome", "not-yours")
            return
        # No `gone` (AHC-0096): Chatwoot already has its 202, and the reply is
        # posted into a conversation that keeps it. A customer who closes the
        # widget reads it when they come back, so there is no caller to lose.
        try:
            result, after = await channel.agent.handle(
                message.text,
                identity=who,
                conversation=conversation,
                delivery_id=f"chatwoot:{message.account}:{message.message}",
            )
        except req.RequestRefused:
            span.set_attribute("agent.channel.outcome", "duplicate")
            return
        span.set_attribute("agent.channel.outcome", type(result).__name__.lower())
        await api.reply(
            *where, getattr(result, "reply", "") or getattr(result, "customer_message", "")
        )
        if isinstance(result, Escalated):
            await api.note(*where, f"Why: {result.reason}\n{after.facts.as_handoff()}")
            await api.hand_off(*where)
        if isinstance(result, NeedsApproval):
            # A doorbell, never a verdict (T-059). The note says what is waiting
            # and where to decide it; the decision happens at the desk, as a
            # named person, through the rule the workflow holds. Nothing here
            # can grant anything, and a note that could would be a link a mail
            # scanner could click.
            await api.note(*where, _waiting_note(result))
            await api.hand_off(*where)


def _waiting_note(result: NeedsApproval) -> str:
    """What a colleague reads in the inbox: what is waiting, and where it is."""
    return (
        f"Waiting for an approval — {result.action}.\n"
        f"Reason: {result.reason}\n"
        f"Reference: {result.approval_id}\n"
        "Decide it at the desk (/ops/desk). Nothing has been done yet."
    )


async def _customer(channel: Channel, message: Incoming) -> Identity | str:
    """The customer's live session, or the sentence turning the message away:
    sign in, or send something shorter."""
    if not message.verified:
        return SIGN_IN
    if len(message.text) > MAX_TEXT:
        # Before a session is looked up or anything is paid for (AHC-0016).
        # /chat refuses a body over 8 KiB; this door had no bound, and the trim
        # never drops the latest turn, so a paste of any size reached the model
        # (F-068).
        return TOO_LONG
    try:
        return await channel.sessions.identity_for(message.subject)
    except (ident.SessionEnded, ident.NotACustomer):
        return SIGNED_OUT


async def _conversation(
    store: CheckpointStore, message: Incoming, who: Identity
) -> Conversation | None:
    """This Chatwoot conversation's record, named by Chatwoot's ids so no mapping
    table is needed. `None` when it belongs to another customer."""
    cid = ConversationId(f"cw-{message.account}-{message.conversation}")
    previous = await store.latest(cid)
    if previous is None:
        return Conversation(conversation_id=cid, customer_id=who.customer_id)
    conversation = Conversation.decode(previous)
    return conversation if conversation.customer_id == who.customer_id else None


def build(channel: Channel) -> Starlette:
    """The receiver, ready to mount at /chatwoot. Wiring only."""
    app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
    app.state.channel = channel
    return app


__all__ = [
    "Channel",
    "ChatwootApi",
    "Inbox",
    "ChatwootClient",
    "Incoming",
    "MAX_TEXT",
    "Opening",
    "TOO_LONG",
    "build",
    "greet",
    "incoming",
    "opened",
]


@dataclass(frozen=True)
class Inbox:
    """Where a waiting approval is announced: the conversation it came from.

    A conversation id is `cw-<account>-<conversation>` — the channel's own ids,
    so a reminder finds the inbox with no mapping table to keep (T-059). An
    approval raised somewhere else, or from a test, has nothing to announce to
    and is left alone rather than guessed at.

    The note is private: the customer is not told again that somebody is being
    chased about their refund.
    """

    api: ChatwootApi
    desk_url: str = "/ops/desk"

    async def waiting(self, approval: Approval) -> None:
        where = _addressed(approval.conversation_id)
        if where is None:
            return
        account, conversation = where
        await self.api.note(
            account, conversation, ap_notify.message(approval, desk_url=self.desk_url)
        )
        await self.api.hand_off(account, conversation)


def _addressed(conversation_id: str) -> tuple[int, int] | None:
    parts = conversation_id.split("-")
    if len(parts) != 3 or parts[0] != "cw":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None
