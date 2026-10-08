"""What a turn records about itself as it opens and as it ends (AHC-0114).

The evaluation record's structure is set on every turn — the join keys, the
versions, the synthetic and captured markers, how the turn ended and by which
rule — and its words only on a turn chosen for capture. Kept apart from the
turn's sequence so that sequence stays readable as a sequence.

The reply screen is here too (`screened`): the last thing a turn does before it
ends is pass its words through the reply guardrails, whichever route made them.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol

from opentelemetry.trace import Span

from agent_harness import policy as pol
from agent_harness import telemetry as tel
from agent_harness.contracts import Completed, Failed, Identity, Refused, Route, RunId, TurnResult
from agent_harness.state import Conversation
from agent_harness.telemetry import counters


class RunLabels(Protocol):
    """What a turn is labelled with from an agent's resolved configuration
    (AACP-0002, AACP-0055). The configuration itself is the agent's."""

    @property
    def fingerprint(self) -> str: ...
    @property
    def resolution(self) -> str: ...


def opened(
    run_id: RunId,
    conversation: Conversation,
    identity: Identity,
    config: RunLabels | None,
    synthetic: bool,
) -> dict[str, str | bool]:
    """Standard names, so a backend groups turns into a conversation and
    attributes them to a customer with no mapping. Emitted here rather than at
    the edge because a turn reaches this point whether it arrived over HTTP or
    from a test, and a join key only some callers produce is one nothing
    downstream can rely on.

    Whether the turn's words are kept was decided once, for the whole turn, by
    `telemetry.turn_scope` around it; this records the decision.
    """
    attributes: dict[str, str | bool] = {
        tel.RUN_ID: run_id,
        tel.SESSION_ID: conversation.conversation_id,
        tel.USER_ID: identity.customer_id,
        tel.SYNTHETIC: synthetic,
        tel.CAPTURED: tel.capturing(),
    }
    if config is not None:
        attributes[tel.CONFIG_FINGERPRINT] = config.fingerprint
        attributes[tel.RESOLUTION] = config.resolution
    return attributes


def fingerprint(config: RunLabels | None) -> str | None:
    """The configuration a turn's numbers are labelled with (AACP-0002, AACP-0055)."""
    return config.fingerprint if config is not None else None


def closed(
    span: Span, result: TurnResult, decision: Route | None, started: float, synthetic: bool
) -> None:
    """How the turn ended, onto its span and into the numbers — once, at the end.

    A turn held by work in someone else's hands never reached the router; it is
    recorded on the span and not counted, as before, because the rates are over
    turns the agent decided.
    """
    span.set_attribute(tel.TURN_RESULT, result.kind)
    rule = getattr(result, "rule_id", None)
    if rule:
        span.set_attribute(tel.TURN_RULE, rule)
    reply = getattr(result, "reply", "") or getattr(result, "customer_message", "") or ""
    span.set_attribute(tel.REPLY_REDACTED, tel.redact(reply) != reply)
    tel.set_payload(span, tel.REPLY, reply)
    if decision is not None:
        counters.record_turn(
            result, decision, duration_s=time.monotonic() - started, synthetic=synthetic
        )


def as_answer(result: TurnResult, conversation: Conversation) -> dict[str, object]:
    """What a redelivery of this turn is told — the same three fields `/chat`
    answers with, kept so the second arrival gets the answer rather than the
    news that one exists.

    Here rather than in `serve`, because a delivery may arrive over any
    transport and the answer belongs to the turn, not to HTTP. `serve` renders
    it; the channel could too.
    """
    return {
        "conversation_id": conversation.conversation_id,
        "reply": getattr(result, "reply", "") or getattr(result, "customer_message", ""),
        "outcome": type(result).__name__.lower(),
    }


def screened(
    result: TurnResult,
    identity: Identity,
    rules: Mapping[pol.Position, tuple[pol.Rule, ...]] | None = None,
) -> TurnResult:
    """Every reply passes the reply guardrails before the customer reads it —
    whichever route produced it (F-020). One point, so the next template edit on
    any route is screened without anyone remembering to screen it. The verdict
    and the rule that fired are on the `agent.policy` span `enforce` opens.

    The agent's **configured** rules, not only the built-in ones: this read the
    defaults whatever it was given, so a deployment that added a reply rule was
    screened by the rules it had not configured (F-027)."""
    text = result.customer_message if isinstance(result, Failed) else result.reply
    verdict = pol.enforce(
        pol.Context(position=pol.Position.REPLY, identity=identity, text=text),
        None if rules is None else rules.get(pol.Position.REPLY),
    )
    if not verdict.blocked:
        return result
    # The words are replaced; what the turn *did* is not. An escalation that was
    # raised stays raised and an approval stays pending — replacing the result
    # type would drop the handoff the reply was about.
    if isinstance(result, Failed):
        return result.model_copy(update={"customer_message": pol.SAFE_REPLY})
    if isinstance(result, Completed):
        # A completion carries nothing but its words, and the words were
        # refused — so the result is a refusal. It used to stay `Completed`
        # with a `REFUSED` termination, which a caller branching on the type
        # read as a success (F-026, AHC-0017).
        return Refused(reply=pol.SAFE_REPLY, reason=verdict.reason, rule_id=verdict.rule)
    return result.model_copy(update={"reply": pol.SAFE_REPLY})


__all__ = ["RunLabels", "as_answer", "closed", "opened", "screened"]
