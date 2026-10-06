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
from support_agent.contracts.reading import ORDER_ID, order_ids

_MONEY = r"(?:refunds?|money\s+back|credit|compensation)\b"
"""What a customer calls money owed back to them (T-094)."""


@dataclass(frozen=True)
class Rules:
    """Routing rules are versioned configuration, not code you edit casually —
    AAC-0101 gates routing changes like model changes."""

    version: str = "v2"
    """v2 (17 Sep): R-DISCOUNT no longer refuses a customer describing a voucher or
    coupon they paid with (T-050). A routing change is gated like a model change,
    so the version moves and the fingerprint with it."""
    refuse: tuple[tuple[str, str, re.Pattern[str]], ...] = field(
        default_factory=lambda: (
            (
                "R-DISCOUNT",
                "discount negotiation is not something I can do",
                # The spec says *negotiating* price or *offering* a discount.
                # "And if I paid partly with a voucher" is neither: it describes
                # how an order was paid, and v1 refused it on the noun, live, in
                # the tenth turn of a returns conversation, which then counted as
                # the customer's first refusal and fetched a second person
                # (T-050). Using, paying with or a code that did not apply is
                # carved out; asking is not.
                re.compile(
                    r"^(?!.*\b(?:paid|pay|paying|used|using|applied|redeemed|with)\b"
                    r"[^.?!]{0,20}\b(?:voucher|coupon|gift card|discount code)\b)"
                    r"(?!.*\b(?:voucher|coupon|discount)\s+code\s+"
                    r"(?:did ?n[o'’]?t|does ?n[o'’]?t|won'?t|is ?n[o'’]?t)\b)"
                    # Plurals too: "any coupons?" slipped past v1 and v2's first draft.
                    r".*\b(discounts?|coupons?|vouchers?|price match|cheaper)\b",
                    re.I | re.S,
                ),
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
                # "my card was charged twice" is not an accusation to refuse: it
                # is a billing dispute, and since 6 Oct a person's (outside-scope).
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
                # The AOAS's own examples (T-094): "My parcel is lost", and
                # "Tracking says delivered but I never got it" — which went to
                # the status path and was told the parcel was delivered.
                re.compile(
                    r"\blost in transit\b"
                    r"|\b(?:parcel|package|order|item|it)\s+(?:is|was|has been|got)\s+lost\b"
                    r"|\bsays\s+delivered\s+but\b"
                    r"|\bnever\s+(?:got|received|arrived|came)\b",
                    re.I,
                ),
            ),
            (
                "outside-scope",
                "the customer raised a concern this agent has no route for",
                # P-CONCERNS, decided by the owner 6 Oct (T-093): anything the
                # agent cannot handle goes to a person. Billing disputes and
                # warranty are the AOAS's named ones.
                re.compile(
                    r"\b(?:charged|billed)\s+twice\b|\bdouble[- ]charged?\b"
                    r"|\bpayment\b[^.?!]{0,40}\b(?:don'?t|do\s+not)\s+recogni[sz]e\b"
                    r"|\bwarrant(?:y|ies)\b",
                    re.I,
                ),
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
            (
                Intent.CANCEL_ORDER,
                re.compile(r"\bcancel\b|\bdon'?t\s+want\s+(?:this|the|my)\s+order\b", re.I),
            ),
            (
                Intent.RETURN_REQUEST,
                re.compile(r"\breturn\b|\bsend\s+(?:it|this|them)\s+back\b", re.I),
            ),
            (Intent.EXCHANGE_REQUEST, re.compile(r"\b(exchange|swap|different size)\b", re.I)),
            (
                Intent.REFUND_STATUS,
                # Anchored on *asking about* a refund, never on the noun. The
                # bare word matched "please refund my order", so a request for
                # money back was answered with its status — true, useless, and
                # not what was asked (F-030). Third time this shape of defect
                # has appeared: the escalate rule and R-STYLE both learned it.
                #
                # And on the money, whatever it is called (T-094, a CCA-F case):
                # "where is my money back" went to the parcel's status, and
                # "credit" and "compensation" questions reached the model with
                # no intent, free to request a refund nobody asked for.
                re.compile(
                    rf"\b(where|when|what|how)\b[^.?!]{{0,30}}\b{_MONEY}"
                    rf"|\b{_MONEY}[^.?!]{{0,24}}\b(status|update|yet|processed|arrived?|coming)\b"
                    rf"|\b(did|has|have)\b[^.?!]{{0,20}}\b{_MONEY}[^.?!]{{0,20}}\b(go|gone|come|came)\b"
                    rf"|\bany\s+news\b[^.?!]{{0,30}}\b{_MONEY}"
                    r"|\b(been|was|is)\s+refunded\b"
                    r"|\brefund\s+status\b",
                    re.I,
                ),
            ),
            (
                Intent.REFUND_REQUEST,
                # Asking for the money, not about it. The AOAS's `refund_request`
                # had no intent here at all until T-094.
                re.compile(
                    r"\b(?:want|need|like|give\s+me|get)\b[^.?!]{0,20}"
                    r"\b(?:my\s+money\s+back|money\s+back|a\s+refund|compensation)\b"
                    r"|\brefund\s+(?:my|this|the|it)\b",
                    re.I,
                ),
            ),
            (
                Intent.ADDRESS_CHANGE,
                re.compile(
                    r"\b(change|update).{0,20}address\b|\b(?:ship|send)\s+(?:it|this|them)\s+to\b",
                    re.I,
                ),
            ),
            (Intent.DAMAGED_ITEM, re.compile(r"\b(damaged|broken|missing|torn|cracked)\b", re.I)),
            (
                Intent.ORDER_STATUS,
                # Not when the subject is a refund: "where is my refund" is a
                # question about money, and matching both intents made it
                # ambiguous — so the one utterance P-REFUND-STATUS exists for
                # went to the loop (F-030).
                re.compile(
                    # Nor when it is about money or damage: "it arrived torn" is
                    # a damaged item, and "where is my money back" a refund (T-094).
                    r"^(?!.*\b(?:refunds?|money\s+back|credit|compensation|damaged|broken|torn|cracked)\b)"
                    r".*\b(where is|status|track|delivered|arriv|shipped)\w*\b",
                    re.I | re.S,
                ),
            ),
            (Intent.POLICY_QUESTION, re.compile(r"\b(policy|how long|window|allowed)\b", re.I)),
        )
    )


_CLAUSE = re.compile(
    r"(?<=[.?!;])\s+|,\s*(?:and\s+)?|\s+and\s+(?=(?:i|i'm|i've|is|was|my|the|it|can|could"
    r"|please|where|what|when|how|why|also)\b)",
    re.I,
)


def concerns(text: str) -> tuple[str, ...]:
    """The separate things a message raises, one per clause (AHC-0118, T-093).

    A CCA-F case: customers open with several problems — a cracked hinge, a
    double charge, a warranty question — and the first reply covered one, and
    the person who later took the conversation found no record of the others.
    Split here so the record holds each, and a handoff carries each.

    A clause is a concern when it names an order or runs to three words:
    "Cancel AB-10002" is one, "thanks" and "that's all" are not.
    Splitting is by punctuation and a new clause after "and", which is crude and
    errs towards keeping a concern whole rather than cutting one in two.
    """
    parts = (part.strip(" ,.;") for part in _CLAUSE.split(text))
    return tuple(part for part in parts if ORDER_ID.search(part) or len(part.split()) >= 3)


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
    #
    # Found however it is written — lower case, or with the look-alike hyphen
    # the agent itself writes (F-048) — and handed on in the store's spelling.
    # Exact-match only, "ab-10003" went to the model (F-063).
    found = order_ids(text)
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


__all__ = ["concerns", "DIRECT_HANDLERS", "ORDER_ID", "Rules", "refusal_text", "route"]
