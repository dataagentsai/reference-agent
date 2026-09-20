"""What a turn records about itself as it opens and as it ends (AHC-0114).

The evaluation record's structure is set on every turn — the join keys, the
versions, the synthetic and captured markers, how the turn ended and by which
rule — and its words only on a turn chosen for capture. Kept apart from the
turn's sequence so that sequence stays readable as a sequence.
"""

from __future__ import annotations

import time

from opentelemetry.trace import Span

from support_agent import telemetry as tel
from support_agent.config import RunConfig
from support_agent.contracts import Identity, Route, RunId, TurnResult
from support_agent.state import Conversation
from support_agent.telemetry import counters


def opened(
    run_id: RunId,
    conversation: Conversation,
    identity: Identity,
    config: RunConfig | None,
    synthetic: bool,
) -> dict[str, str | bool]:
    """Standard names, so a backend groups turns into a conversation and
    attributes them to a customer with no mapping. Emitted here rather than at
    the edge because a turn reaches this point whether it arrived over HTTP or
    from a test, and a join key only some callers produce is one nothing
    downstream can rely on.

    Whether the turn's words are kept is decided here, once, for the whole turn.
    """
    attributes: dict[str, str | bool] = {
        tel.RUN_ID: run_id,
        tel.SESSION_ID: conversation.conversation_id,
        tel.USER_ID: identity.customer_id,
        tel.SYNTHETIC: synthetic,
        tel.CAPTURED: tel.begin_capture(run_id),
    }
    if config is not None:
        attributes[tel.CONFIG_FINGERPRINT] = config.fingerprint
        attributes[tel.RESOLUTION] = config.resolution
    return attributes


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


__all__ = ["closed", "opened"]
