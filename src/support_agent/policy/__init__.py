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

REFUND_CLAIM = re.compile(
    r"\b(?:i(?:'ve| have)?\s+)?(?:refunded|issued (?:your|the|a) refund|"
    r"refund (?:has been|is) (?:issued|processed|completed|sent))\b",
    re.I,
)
CARD = re.compile(r"\b\d{13,19}\b")
DISCOUNT_OFFER = re.compile(r"\b(\d{1,2}%\s*(off|discount)|voucher|coupon code)\b", re.I)
DATE_PROMISE = re.compile(r"\b(?:will (?:arrive|be delivered)|delivery (?:is|will be) on)\b", re.I)


def no_unclaimed_refund(ctx: Context) -> Verdict:
    """A refund may be *claimed* only if one actually happened this turn.

    The gate stops an unauthorised refund; this stops the agent telling the
    customer it did one anyway. Both failures cost the same at the support desk,
    and only one of them is visible in the ledger.
    """
    if not REFUND_CLAIM.search(ctx.text):
        return ALLOW
    refunded = any(r.name == "issue_refund" and not r.is_error for r in ctx.tool_results)
    if refunded:
        return ALLOW
    return block("no_unclaimed_refund", "claimed a refund that did not happen")


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
    no_unclaimed_refund,
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
    "no_unclaimed_refund",
]
