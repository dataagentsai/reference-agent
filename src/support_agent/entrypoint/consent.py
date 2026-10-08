"""What the customer has authorised, read from what they said (T-050).

The pre-tool rule `policy.customer_asked` lets an action run only on an order the
customer asked for it on. This module works out what they asked, from their own
messages in the conversation and never from a tool's result, so text planted in
an order, a note or a product description cannot add to it.

Two ways in, as the user decided on 17 September:

- **Asked.** A customer message names the action (the router's own intent
  patterns, so there is one opinion about intent, not two) and an order: that
  message's order ids, or, if it names none, the orders the customer has named
  anywhere in the conversation.
- **Confirmed.** An action the rule refused is recorded as awaiting
  confirmation; a plainly affirmative next message ("yes", "go ahead") authorises
  exactly those, and any other message withdraws them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from agent_harness.state import Conversation
from agent_harness.state.facts import Facts
from support_agent import router
from support_agent.contracts import Identity, Intent
from support_agent.contracts.reading import order_ids

BY_INTENT = {
    "cancel_order": Intent.CANCEL_ORDER,
    "open_return_request": Intent.RETURN_REQUEST,
    "change_address": Intent.ADDRESS_CHANGE,
}
REFUND_ASKED = re.compile(r"\b(refund|money back)\b", re.I)
"""Asking for money back. A question about a refund's status is `REFUND_STATUS`
and authorises nothing (F-030)."""

AFFIRMATIVE = re.compile(
    r"^\s*(yes|yeah|yep|sure|ok(ay)?|please do|go ahead|confirm(ed)?|do it)\b", re.I
)
CONFIRM = "confirm"


def granting(
    identity: Identity, conversation: Conversation, text: str, rules: router.Rules
) -> Identity:
    """The turn's identity, carrying what the customer has consented to."""
    return identity.model_copy(update={"consented": consented(conversation, text, rules)})


def consented(conversation: Conversation, text: str, rules: router.Rules) -> frozenset[str]:
    said = [m.content for m in conversation.messages if m.role == "user"]
    if not said or said[-1] != text:
        said.append(text)
    everywhere = {o for s in said for o in order_ids(s)}
    granted: set[str] = set()
    for message in said:
        intents = {intent for intent, pattern in rules.intents if pattern.search(message)}
        orders = order_ids(message) or everywhere
        wanted = [tool for tool, intent in BY_INTENT.items() if intent in intents]
        if REFUND_ASKED.search(message) and Intent.REFUND_STATUS not in intents:
            wanted.append("request_refund")
        granted |= {f"{tool}:{order}" for tool in wanted for order in orders}
    if AFFIRMATIVE.match(text):
        granted |= set(awaiting(conversation.facts))
    return frozenset(granted)


def awaiting(facts: Facts) -> list[str]:
    prefix = f"{CONFIRM}:"
    return [entry[len(prefix) :] for entry in facts.awaiting if entry.startswith(prefix)]


def pending(facts: Facts, attempted: Iterable[tuple[str, str]], identity: Identity) -> Facts:
    """This turn's refused actions become the ones awaiting confirmation; any
    earlier ones are withdrawn, so a yes three turns later authorises nothing."""
    for entry in awaiting(facts):
        facts = facts.settled(CONFIRM, entry)
    for name, arguments in attempted:
        if name not in (*BY_INTENT, "request_refund"):
            continue
        given = json.loads(arguments) if arguments.startswith("{") else {}
        entry = f"{name}:{given.get('id') or given.get('order_id')}"
        if entry not in identity.consented:
            facts = facts.waiting_on(CONFIRM, entry)
    return facts


__all__ = ["consented", "granting", "pending"]
