"""Whether the model is needed at all.

Spans L4 and L7 without belonging to either. Runs **per user turn, outside the
loop**, and owns the decision of whether the loop runs.

Per turn, not per conversation: this is an async, multi-turn system and intent
drifts — *"actually, also cancel my other order"* arrives three turns in.
Classifying once at the start is wrong.

Outside the loop, always. If the model could invoke the router, the router's
determinism would be gone and every path would be agentic again. It is a pure
decision function: it returns a `Route` and never calls the loop, which the
`router | loop` independence contract enforces rather than leaving to discipline.

**`Refuse` and `Escalate` never cost a loop iteration.** "Give me a discount" and
"get me a human" resolve deterministically, free, and binary, at stage S2. That
is deterministic-first as a structural property rather than a slogan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import assert_never

from support_agent import telemetry as tel
from support_agent.contracts import Agentic, Direct, Escalate, Intent, Refuse, Route

ORDER_ID = re.compile(r"\b([A-Z]{1,3}-\d{3,8})\b")


@dataclass(frozen=True)
class Rules:
    """Routing rules are versioned configuration, not code you edit casually —
    AAC-0101 gates routing changes like model changes."""

    version: str = "v1"
    refuse: tuple[tuple[str, str, re.Pattern[str]], ...] = field(
        default_factory=lambda: (
            (
                "R-DISCOUNT",
                "discount negotiation is not something I can do",
                re.compile(r"\b(discount|coupon|voucher|price match|cheaper)\b", re.I),
            ),
            (
                "R-OTHER-CUSTOMER",
                "I cannot discuss another customer's order",
                re.compile(r"\b(someone else'?s|my friend'?s|another customer)\b", re.I),
            ),
            (
                "R-ACCOUNT",
                "account and payment changes are not something I can do",
                re.compile(r"\b(delete my account|change my (card|payment))\b", re.I),
            ),
            (
                "R-STYLE",
                "I cannot give style or fit advice",
                # Anchored on *asking for the advice*, never on the nouns: "the
                # fit was wrong, I want to return it" is a return, and the
                # escalate rule above is here because that lesson cost a defect.
                re.compile(
                    r"\b(?:what|which)\s+size\b"
                    r"|\bsize\s+(?:up|down)\b"
                    # The subject may be a noun, not only a pronoun. This read
                    # `(it|this|that|they|these)` and so missed "does this jacket
                    # suit me" and "will the medium fit" — the two commonest ways
                    # anybody asks (F-038). Still anchored on the question word,
                    # so "the fit was wrong, I want to return it" is untouched:
                    # it opens with no will/would/does/do.
                    r"|\b(?:will|would|does|do)\s+(?:\w+\s+){1,3}"
                    r"(?:fit|suit|look)\b"
                    r"|\bshould\s+i\s+(?:get|buy|order|choose|pick)\b"
                    r"|\b(?:style|fashion|fit|sizing)\s+advice\b"
                    r"|\bwhat\s+(?:do\s+you\s+think|would\s+you\s+recommend)\b",
                    re.I,
                ),
            ),
            (
                "R-FRAUD",
                "I cannot judge whether something is fraud",
                # Adjudication, which is what the spec refuses — not a report.
                # "my card was charged twice" is a question this agent answers.
                re.compile(
                    r"\b(?:is|was|isn'?t|wasn'?t)\s+(?:this|that|it|the|my)\s*"
                    r"(?:\w+\s+){0,2}(?:fraud|fraudulent|scam|stolen)\b"
                    r"|\b(?:do|can)\s+you\s+think\s+.{0,30}\bfraud\b"
                    r"|\b(?:confirm|decide|determine|tell\s+me)\b.{0,30}\bfraud\b",
                    re.I,
                ),
            ),
        )
    )
    """The AOAS `refuses` list, each with the id it enforces. R-DELIVERY-DATE is
    not here: a delivery date is refused where one could be *invented*, by the
    reply guardrail, because no phrasing of the question is the problem."""
    escalate: tuple[tuple[str, str, re.Pattern[str]], ...] = field(
        default_factory=lambda: (
            (
                "asked-for-human",
                "the customer asked for a human",
                # Anchored on the *request*, not on the noun. The first version
                # of this matched `\b(human|agent|manager|...)\b` anywhere in the
                # turn, so "the delivery agent left it at the wrong door" — an
                # ordinary missing-item turn this agent is built to handle — was
                # escalated to a person. Over-escalation with no way to see it:
                # the route was recorded as correct because the rule did fire.
                #
                # So a verb of asking must appear with the noun. `transfer me`
                # and `escalate` stand alone because neither has an innocent
                # reading in a support conversation.
                re.compile(
                    r"\b(?:speak|talk|chat)(?:ing)?\s+(?:to|with)\s+(?:a|an|the)?\s*"
                    r"(?:human|person|agent|manager|supervisor|someone|somebody"
                    r"|representative|rep)\b"
                    r"|\b(?:put|get|pass|connect|transfer)\s+me\b"
                    r"|\b(?:want|need)\s+(?:a|an|the)\s+"
                    r"(?:human|person|agent|manager|supervisor|someone|somebody"
                    r"|representative|rep)\b"
                    r"|\breal\s+(?:person|human)\b"
                    r"|\bhuman\s+being\b"
                    r"|\bescalate\b",
                    re.I,
                ),
            ),
            (
                "lost-in-transit",
                "the item is reported lost in transit",
                re.compile(r"\blost in transit\b", re.I),
            ),
        )
    )
    """Two rules, and they are not the same kind of thing.

    The first is the customer asking. The second is a **policy** escalation — a
    case class this agent may not resolve alone — and nobody asked for it. Tier 2
    of the design is a whole family of the second kind, derived from conversation
    state rather than from text; these are the two that can be decided from the
    turn alone.
    """
    intents: tuple[tuple[Intent, re.Pattern[str]], ...] = field(
        default_factory=lambda: (
            (Intent.CANCEL_ORDER, re.compile(r"\bcancel\b", re.I)),
            (Intent.RETURN_REQUEST, re.compile(r"\breturn\b", re.I)),
            (Intent.EXCHANGE_REQUEST, re.compile(r"\b(exchange|swap|different size)\b", re.I)),
            (
                Intent.REFUND_STATUS,
                # Anchored on *asking about* a refund, never on the noun. The
                # bare word matched "please refund my order", so a request for
                # money back was answered with its status — true, useless, and
                # not what was asked (F-030). Third time this shape of defect
                # has appeared: the escalate rule and R-STYLE both learned it.
                re.compile(
                    r"\b(where|when|what|how)\b[^.?!]{0,30}\brefunds?\b"
                    r"|\brefunds?\b[^.?!]{0,24}\b(status|update|yet|processed|arrived?|coming)\b"
                    r"|\b(been|was|is)\s+refunded\b"
                    r"|\brefund\s+status\b",
                    re.I,
                ),
            ),
            (Intent.ADDRESS_CHANGE, re.compile(r"\b(change|update).{0,20}address\b", re.I)),
            (Intent.DAMAGED_ITEM, re.compile(r"\b(damaged|broken|missing|torn)\b", re.I)),
            (
                Intent.ORDER_STATUS,
                # Not when the subject is a refund: "where is my refund" is a
                # question about money, and matching both intents made it
                # ambiguous — so the one utterance P-REFUND-STATUS exists for
                # went to the loop (F-030).
                re.compile(
                    r"^(?!.*\brefunds?\b).*\b(where is|status|track|delivered|arriv)\w*\b",
                    re.I | re.S,
                ),
            ),
            (Intent.POLICY_QUESTION, re.compile(r"\b(policy|how long|window|allowed)\b", re.I)),
        )
    )


DIRECT_HANDLERS: dict[Intent, str] = {
    Intent.ORDER_STATUS: "order_status",
    Intent.REFUND_STATUS: "refund_status",
}
"""Only read-only, single-step intents may resolve without the model.

Anything that writes goes through the loop, where the tool boundary can mint an
idempotency key and the trajectory is visible. A deterministic path is cheaper;
it is not a place to hide an irreversible effect.
"""


def route(text: str, *, rules: Rules | None = None) -> Route:
    """Classify one turn.

    Order matters. Refusal and escalation are checked before intent, because a
    request that is out of scope does not become in scope by also mentioning an
    order, and a customer asking for a human should not be routed into a loop
    that will try to help first.
    """
    rules = rules or Rules()
    with tel.span("agent.route", **{tel.ROUTE_KIND: "pending"}) as span:
        decision = _decide(text, rules)
        span.set_attribute(tel.ROUTE_KIND, decision.kind)
        span.set_attribute(tel.ROUTE_REASON, _reason_of(decision))
        span.set_attribute("agent.router.rules_version", rules.version)
        return decision


def _decide(text: str, rules: Rules) -> Route:
    for rule_id, reason, pattern in rules.refuse:
        if pattern.search(text):
            return Refuse(
                reason=reason,
                alternative="I can help with orders, returns and refunds.",
                rule_id=rule_id,
            )

    for rule_id, reason, pattern in rules.escalate:
        if pattern.search(text):
            return Escalate(reason=reason, rule_id=rule_id, tier=1)

    matched = tuple(intent for intent, pattern in rules.intents if pattern.search(text))

    # Ambiguity goes to the model. The loop is the fallback, not the default —
    # so an unrecognised turn and a multi-intent turn take the same path.
    if len(matched) != 1:
        return Agentic(goal=text.strip(), candidate_intents=matched)

    intent = matched[0]
    handler = DIRECT_HANDLERS.get(intent)
    if handler is None:
        return Agentic(goal=text.strip(), candidate_intents=matched)

    # Direct only when everything the handler needs is already present, and
    # present once. A deterministic path that has to ask a question is not
    # deterministic — and one that has to *choose* is worse, because it does not
    # ask, it picks.
    #
    # `P-DIRECT` says one intent and one order. Only the first half was checked:
    # this read the first id in the turn and ignored the rest, so "where is my
    # order AB-10003 and what about AB-10004 and AB-10005" was answered about
    # AB-10003 with nothing said about the other two (F-037). Several orders is
    # the same ambiguity as several intents and takes the same path.
    found = {match.group(1) for match in ORDER_ID.finditer(text)}
    if len(found) != 1:
        return Agentic(goal=text.strip(), candidate_intents=matched)

    return Direct(intent=intent, handler=handler, args={"order_id": found.pop()})


def _reason_of(decision: Route) -> str:
    match decision:
        case Refuse() | Escalate():
            return decision.reason
        case Direct():
            return f"unambiguous {decision.intent.value} with an order id"
        case Agentic():
            return "ambiguous, multi-intent or unmodelled"
        case _:
            # A new route kind fails the type check here, rather than being
            # described as whichever case happened to be last.
            assert_never(decision)


def refusal_text(decision: Refuse) -> str:
    """What a refused customer is told — the reason, and the offer if there is one."""
    if decision.alternative:
        return f"I am sorry — {decision.reason}. {decision.alternative}"
    return f"I am sorry — {decision.reason}."


__all__ = ["DIRECT_HANDLERS", "ORDER_ID", "Rules", "refusal_text", "route"]
