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

from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent import trigger as trg
from support_agent.contracts import CheckpointStore, ConversationId, Escalated, Identity
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

    async def _post(self, account: int, conversation: int, path: str, body: dict[str, Any]) -> None:
        url = f"{self._base}/api/v1/accounts/{account}/conversations/{conversation}/{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "api_access_token": self._token},
        )
        await asyncio.to_thread(urllib.request.urlopen, request, timeout=15)  # noqa: S310


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


async def webhook(request: Request) -> Response:
    channel: Channel = request.app.state.channel
    raw = await request.body()
    with tel.span("agent.channel.webhook") as span:
        if not channel.signed(request.headers, raw):
            span.set_attribute("agent.channel.outcome", "unsigned")
            return JSONResponse({"error": "not signed"}, status_code=401)
        try:
            message = incoming(json.loads(raw))
        except ValueError:
            message = None
        if message is None:
            span.set_attribute("agent.channel.outcome", "ignored")
            return JSONResponse({"status": "ignored"})
        span.set_attribute("agent.channel.outcome", "accepted")
    return JSONResponse(
        {"status": "accepted"}, status_code=202, background=BackgroundTask(answer, channel, message)
    )


async def answer(channel: Channel, message: Incoming) -> None:
    """The turn, after Chatwoot has its 202."""
    api, where = channel.api, (message.account, message.conversation)
    with tel.span("agent.channel.turn") as span:
        who = await _customer(channel, message)
        if isinstance(who, str):
            span.set_attribute("agent.channel.outcome", "sign-in")
            await api.reply(*where, who.format(portal=channel.portal_url))
            return
        conversation = await _conversation(channel.store, message, who)
        if conversation is None:
            span.set_attribute("agent.channel.outcome", "not-yours")
            return
        try:
            result, after = await channel.agent.handle(
                message.text,
                identity=who,
                conversation=conversation,
                delivery_id=f"chatwoot:{message.account}:{message.message}",
            )
        except (trg.DuplicateDelivery, trg.OverlappingRun):
            span.set_attribute("agent.channel.outcome", "duplicate")
            return
        span.set_attribute("agent.channel.outcome", type(result).__name__.lower())
        await api.reply(
            *where, getattr(result, "reply", "") or getattr(result, "customer_message", "")
        )
        if isinstance(result, Escalated):
            await api.note(*where, after.facts.as_handoff())
            await api.hand_off(*where)


async def _customer(channel: Channel, message: Incoming) -> Identity | str:
    """The customer's live session, or the sentence telling them to sign in."""
    if not message.verified:
        return SIGN_IN
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


__all__ = ["Channel", "ChatwootApi", "ChatwootClient", "Incoming", "build", "incoming"]
