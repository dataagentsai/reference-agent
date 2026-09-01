"""Where a rule becomes real.

L7 · P3 and P5. Four enforcement points, and the position matters as much as the
rule: the same check applied before a tool call and after a model reply catches
different failures and misses different ones.

    PRE_MODEL   what we are about to ask
    POST_MODEL  what the model said           ← the one nothing else covers
    PRE_TOOL    an action about to be taken
    POST_TOOL   what came back

Until this module existed the only controls were the router, on input, and the
tool boundary, on action. **Nothing checked what the model said** — which is most
of test family F6, and the difference between an agent that cannot issue an
unauthorised refund and one that cannot *claim* it did.

### Fails closed

AAC-0091. A rule that raises blocks the traffic it was inspecting. The
alternative — logging the error and passing the content through — produces a
guardrail that is believed and absent at the same time, which is worse than
having none, because nobody goes looking for the control they think they have.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from support_agent import telemetry as tel
from support_agent.contracts import Identity, ToolResult


class Position(StrEnum):
    PRE_MODEL = "pre_model"
    POST_MODEL = "post_model"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"


@dataclass(frozen=True)
class Context:
    """What a rule may look at. Deliberately narrow — a rule that needs more than
    this is probably a business decision wearing a guardrail's coat."""

    position: Position
    identity: Identity
    text: str = ""
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    tool_results: tuple[ToolResult, ...] = ()
    """Everything the tools returned this turn. This is what makes a grounding
    check possible at all: a claim can be compared against what actually
    happened rather than against a rubric."""


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: str = ""
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


ALLOW = Verdict(allowed=True)


def block(rule: str, reason: str) -> Verdict:
    return Verdict(allowed=False, rule=rule, reason=reason)


# --------------------------------------------------------------------------- #
# Rules. Each is a plain callable returning a Verdict.
# --------------------------------------------------------------------------- #

CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "issue_refund": re.compile(
        r"\b(?:i|we)(?:'ve| have|'ll| will)?\s+(?:now\s+)?refunded\b"
        r"|\b(?:i|we)(?:'ve| have)?\s+issued\s+(?:your|the|a)\s+refund\b"
        r"|\b(?:your|the|that)\s+refund\s+(?:has been|was|is now)\s+"
        r"(?:issued|processed|completed|sent)\b",
        re.I,
    ),
    "cancel_order": re.compile(
        r"\b(?:i|we)(?:'ve| have)?\s+(?:now\s+)?cancell?ed\b"
        r"|\b(?:your|the|that|this)?\s*(?:order|it)\s+(?:has been|was|is now)\s+cancell?ed\b",
        re.I,
    ),
    "dispatch_replacement": re.compile(
        r"\b(?:i|we)(?:'ve| have)?\s+(?:sent|dispatched|shipped)\s+"
        r"(?:a |your )?replacement\b",
        re.I,
    ),
}
"""How a claim about each irreversible action looks in prose.

Per-action because the verbs differ, and *complete with respect to the registry*
because a test fails when an irreversible tool has no entry here. F-001 happened
because a guardrail that names one action protects one action.

**Each pattern requires an affirmative construction**, not merely the verb.
F-004: the first version matched the bare word, so a correct refusal — *"that
order has shipped, so it can no longer be cancelled"* — was blocked and the
customer was handed to a colleague for no reason. A guardrail that blocks correct
behaviour is worse than one that misses a claim, because it fires constantly and
is therefore switched off.
"""

CARD = re.compile(r"\b\d{13,19}\b")
DISCOUNT_OFFER = re.compile(r"\b(\d{1,2}%\s*(off|discount)|voucher|coupon code)\b", re.I)
DATE_PROMISE = re.compile(r"\b(?:will (?:arrive|be delivered)|delivery (?:is|will be) on)\b", re.I)


def no_unclaimed_effect(ctx: Context) -> Verdict:
    """An irreversible action may be *claimed* only if it actually happened.

    The approval gate stops an unauthorised effect; this stops the agent telling
    the customer it did one anyway. Both failures cost the same at the support
    desk, and only one of them is visible in the ledger.

    Checked against what the tools returned **this turn**, not against what the
    model believed when it started — which is what F-002 turned on: the agent had
    read `pending`, the world moved, the write was refused, and the reply came
    from the stale expectation.

    A refusal is not a success. A tool that returned `allowed: false` did not do
    the thing, and neither did one that errored.
    """
    for action, pattern in CLAIM_PATTERNS.items():
        if not pattern.search(ctx.text):
            continue
        if not any(_succeeded(r, action) for r in ctx.tool_results):
            return block("no_unclaimed_effect", f"claimed {action} happened when it did not")
    return ALLOW


def _succeeded(result: ToolResult, action: str) -> bool:
    if result.name != action or result.is_error:
        return False
    structured = result.structured
    return not (isinstance(structured, dict) and structured.get("allowed") is False)


def no_invented_delivery_date(ctx: Context) -> Verdict:
    """A delivery promise must be supported by something a tool returned.

    Crude — substring containment against tool output, not entailment. It cannot
    catch a subtly wrong date and does not pretend to. It catches the common
    case, which is a date that appears nowhere but in the model's reply.
    """
    if not DATE_PROMISE.search(ctx.text):
        return ALLOW
    evidence = " ".join(str(r.structured) + r.text for r in ctx.tool_results)
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+\w+\b", ctx.text)
    unsupported = [d for d in dates if d not in evidence]
    if unsupported:
        return block(
            "no_invented_delivery_date",
            f"promised {unsupported[0]!r}, which no tool returned",
        )
    return ALLOW


IDENTIFIER = re.compile(r"\b[A-Z]{1,4}-\d{3,10}\b")
MONEY = re.compile(r"(?:(?:Rs|INR|₹)\s*)([\d,]+(?:\.\d{2})?)|\b(\d[\d,]{2,})\b")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

# Status words were checked here and are not any more. They produced no true
# positives and one false positive: "it can no longer be cancelled" contains
# "cancelled", the tool never returned that status, and a correct refusal was
# blocked. That is F-004 exactly — a guardrail that fires on correct behaviour
# gets switched off, and then protects nothing.
#
# Identifiers, dates and amounts are unambiguous tokens. A status word is
# ordinary English that appears in refusals, questions and policy explanations,
# so grounding it needs meaning rather than membership — which is a judge's job.


def _evidence(results: tuple[ToolResult, ...]) -> str:
    return " ".join(f"{r.structured} {r.text}" for r in results).lower()


def _normalise(amount: str) -> str:
    return amount.replace(",", "").lstrip("0") or "0"


def no_ungrounded_entity(ctx: Context) -> Verdict:
    """Every identifier, amount, date and status word in the reply must appear in
    what the tools actually returned this turn.

    This check exists because `outputSchema` is mandatory. Tool results are
    structured, so "did the model invent this order id" is a set membership
    question rather than a judgement — M1, not M3, and therefore free, exact and
    runnable on every reply rather than sampled.

    It is AAC-0030's *citations resolve* applied to tool output instead of
    documents. Deliberately narrow: it grounds **entities**, not meaning. Three
    things it cannot catch, stated plainly rather than left for someone to
    discover — a reply that negates a true fact ("has *not* been delivered"), one
    that invents a fact with no entity in it ("Bluedart has it"), and one that
    miscounts. All three need a judge, and a judge needs its own validation
    before it can be trusted with anything.

    Digits inside identifiers are not treated as amounts, and amounts are
    compared with separators stripped, because `12,400` and `12400` are the same
    number and a control that says otherwise fires on correct replies — which is
    the F-004 failure mode, and it is the one that gets a guardrail switched off.
    """
    if not ctx.tool_results:
        return ALLOW
    evidence = _evidence(ctx.tool_results)
    text = ctx.text
    identifiers = set(IDENTIFIER.findall(text))

    for found in identifiers:
        if found.lower() not in evidence:
            return block("no_ungrounded_entity", f"cited {found!r}, which no tool returned")

    for date in ISO_DATE.findall(text):
        if date not in evidence:
            return block(
                "no_ungrounded_entity", f"stated the date {date!r}, which no tool returned"
            )

    masked = IDENTIFIER.sub(" ", text)
    for match in MONEY.finditer(masked):
        amount = match.group(1) or match.group(2)
        if amount and _normalise(amount) not in _normalise_all(evidence):
            return block("no_ungrounded_entity", f"stated the figure {amount!r}, unsupported")

    return ALLOW


def _normalise_all(evidence: str) -> str:
    return re.sub(r"[,\s]", "", evidence)


def no_pii_echo(ctx: Context) -> Verdict:
    """Do not read a card number back to the person who typed it.

    They already know it. Repeating it puts it in a transcript, a log and a
    screenshot, which is three places it was not before.
    """
    if CARD.search(ctx.text):
        return block("no_pii_echo", "a card-shaped number appeared in the reply")
    return ALLOW


def no_discount_offer(ctx: Context) -> Verdict:
    """The refusal list in the functional spec is normative, and this is the
    half of it the router cannot enforce: the customer never asked, the model
    volunteered."""
    if DISCOUNT_OFFER.search(ctx.text):
        return block("no_discount_offer", "offered a discount, which is out of scope")
    return ALLOW


OUTPUT_RULES = (
    no_unclaimed_effect,
    no_ungrounded_entity,
    no_invented_delivery_date,
    no_pii_echo,
    no_discount_offer,
)

DEFAULT_RULES: dict[Position, tuple] = {
    Position.PRE_MODEL: (),
    Position.POST_MODEL: OUTPUT_RULES,
    Position.PRE_TOOL: (),
    Position.POST_TOOL: (),
}


# --------------------------------------------------------------------------- #
# Enforcement.
# --------------------------------------------------------------------------- #


def enforce(ctx: Context, rules: Sequence | None = None) -> Verdict:
    """Run the rules for this position. First block wins.

    A rule that raises **blocks**. That is the whole of AAC-0091: a guardrail
    that errors open is believed and absent at once, and nobody goes looking for
    a control they think they have.
    """
    applicable = DEFAULT_RULES.get(ctx.position, ()) if rules is None else rules
    with tel.span("agent.policy", **{"agent.policy.position": ctx.position.value}) as span:
        for rule in applicable:
            name = getattr(rule, "__name__", repr(rule))
            try:
                verdict = rule(ctx)
            except Exception as exc:  # noqa: BLE001 — deliberate: fail closed
                span.set_attribute("agent.policy.blocked_by", name)
                span.set_attribute("agent.policy.errored", True)
                return block(name, f"rule failed and traffic was blocked: {exc}")
            if verdict.blocked:
                span.set_attribute("agent.policy.blocked_by", verdict.rule or name)
                return verdict
        return ALLOW


SAFE_REPLY = "I am not able to confirm that. Let me pass you to a colleague who can help."
"""What the customer sees when a reply is blocked.

Deliberately not an apology for a technical fault and deliberately not silence:
it says nothing false, and it moves the person forward.
"""


__all__ = [
    "ALLOW",
    "DEFAULT_RULES",
    "CLAIM_PATTERNS",
    "OUTPUT_RULES",
    "SAFE_REPLY",
    "Context",
    "Position",
    "Verdict",
    "block",
    "enforce",
    "no_discount_offer",
    "no_invented_delivery_date",
    "no_pii_echo",
    "no_ungrounded_entity",
    "no_unclaimed_effect",
]
