"""The span contract — which spans must carry which attributes, and the check that says so."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from support_agent.telemetry.names import (
    CAPTURED,
    CONFIG_FINGERPRINT,
    CONTEXT_CHARS,
    CONTEXT_EXCHANGES,
    CONTEXT_STORED,
    CONTEXT_TRIMMED,
    COST_CALL_USD,
    COST_USD,
    ESCALATION_ID,
    ESCALATION_RULE,
    ESCALATION_TIER,
    FEEDBACK,
    GEN_AI_INPUT_TOKENS,
    GEN_AI_OPERATION,
    GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_NAME,
    IDEMPOTENCY_KEY,
    INPUT,
    MODEL_MALFORMED,
    REPLY,
    REPLY_REDACTED,
    RESOLUTION,
    ROUTE_KIND,
    ROUTE_REASON,
    RUN_ID,
    SESSION_ID,
    SIDE_EFFECT,
    STEP,
    SYNTHETIC,
    TENANT,
    TERMINATION,
    TOOL_ARGUMENTS,
    TOOL_CALL_BOUND,
    TOOL_OUTCOME,
    TOOL_RESULT,
    TRACER_NAME,
    TURN_RESULT,
    TURN_RULE,
    USER_ID,
)

# --------------------------------------------------------------------------- #
# The span contract.
#
# AAC-0011 says every call emits a **complete** trace. Complete against what?
# Nothing answered that, so "complete" meant whatever each test happened to
# assert. This is the answer: a declaration of which spans exist and what each
# must carry, and a validator that checks it.
#
# An unlisted span name is a violation too. Un-contracted telemetry is telemetry
# nobody can assert over, and it accumulates silently — one span at a time, each
# added for a good reason, until the trace is a place things are written rather
# than a thing that can be checked.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpanSpec:
    """What one span must carry, and what it may."""

    required: frozenset[str]
    optional: frozenset[str] = frozenset()

    def violations(self, name: str, attributes: Mapping[str, Any]) -> list[str]:
        present = set(attributes)
        missing = self.required - present
        unknown = present - self.required - self.optional
        out = [f"{name}: missing {a}" for a in sorted(missing)]
        out += [f"{name}: undeclared attribute {a}" for a in sorted(unknown)]
        return out


CONTRACT: dict[str, SpanSpec] = {
    "agent.turn": SpanSpec(
        # Session and user are required, not optional: both are always in scope
        # by the time this span opens — a conversation is minted if one was not
        # supplied — so anything less than required would let the join key go
        # missing silently, which is the one failure that cannot be repaired
        # after the fact.
        required=frozenset({RUN_ID, SESSION_ID, USER_ID, SYNTHETIC, CAPTURED}),
        # The ending is optional only because a turn that raises has none; every
        # turn that returns sets it (AHC-0114's sixth group).
        optional=frozenset(
            {
                CONFIG_FINGERPRINT,
                RESOLUTION,
                CONTEXT_STORED,
                TURN_RESULT,
                TURN_RULE,
                REPLY_REDACTED,
                INPUT,
                REPLY,
            }
        ),
    ),
    "agent.run": SpanSpec(
        required=frozenset({RUN_ID, TENANT}),
        optional=frozenset(
            {
                TERMINATION,
                COST_USD,
                COST_CALL_USD,
                MODEL_MALFORMED,
                TOOL_CALL_BOUND,
                "agent.policy.blocked_by",
            }
        ),
    ),
    "http.chat": SpanSpec(
        # P1. Nothing is required: a request refused before its token is read
        # has no tenant to record, and demanding one would force the edge to
        # invent a value for exactly the requests it knows least about.
        required=frozenset(),
        # `user.id` and `session.id` on the root as well as the turn: a trace
        # backend groups and filters by the root span (T-052, Langfuse).
        optional=frozenset(
            {
                TENANT,
                USER_ID,
                SESSION_ID,
                "http.status_code",
                "http.refusal_detail",
                "agent.result",
                SYNTHETIC,
            }
        ),
    ),
    "agent.step": SpanSpec(
        required=frozenset({STEP, RUN_ID}),
        optional=frozenset({CONTEXT_CHARS, CONTEXT_EXCHANGES, CONTEXT_TRIMMED}),
    ),
    "agent.route": SpanSpec(
        required=frozenset({ROUTE_KIND, ROUTE_REASON, "agent.router.rules_version"})
    ),
    "agent.direct": SpanSpec(required=frozenset({"agent.handler"})),
    "agent.escalation.raise": SpanSpec(
        # The rule id is required, not optional. An escalation whose span says
        # only "escalated" is one nobody can attribute to a rule, and attributing
        # them to rules is the entire mechanism for telling over-escalation from
        # correct handoff later.
        required=frozenset({ESCALATION_ID, ESCALATION_TIER, ESCALATION_RULE}),
        optional=frozenset({"agent.escalation.rules_version"}),
    ),
    "agent.escalation.lapse": SpanSpec(
        required=frozenset({ESCALATION_ID, ESCALATION_RULE}),
        optional=frozenset({"agent.escalation.waited_s"}),
    ),
    "agent.escalation.wait": SpanSpec(
        required=frozenset({ESCALATION_ID}),
        optional=frozenset({"agent.escalation.waited_s"}),
    ),
    "agent.escalation.queue": SpanSpec(
        # The reader's login, not a tenant. Until T-002 a desk session carried a
        # made-up customer id and this recorded it as `agent.tenant`; a reviewer
        # is no tenant, and naming one as such would put staff reads in every
        # per-tenant panel.
        required=frozenset({USER_ID}),
        optional=frozenset({"agent.escalation.depth"}),
    ),
    "agent.escalation.refused": SpanSpec(
        # Nothing required: a request refused before its token is read has no
        # tenant to record, the same reasoning `http.chat` already carries.
        required=frozenset(),
        optional=frozenset({"http.refusal_detail"}),
    ),
    "agent.channel.webhook": SpanSpec(
        # Before anything is verified there is no tenant to record: the outcome
        # says whether the webhook was unsigned, ignored or accepted (T-026).
        required=frozenset({"agent.channel.outcome"}),
    ),
    "agent.channel.opening": SpanSpec(
        # A widget opened afresh: greeted, not signed in, or a conversation
        # Chatwoot did not mark verified, which gets no greeting (T-001).
        required=frozenset({"agent.channel.outcome"}),
    ),
    "agent.channel.turn": SpanSpec(
        # What came of an accepted message: a turn's result, a request to sign
        # in, a duplicate, or a conversation that is not this customer's.
        required=frozenset({"agent.channel.outcome"}),
    ),
    "agent.opening": SpanSpec(
        # P-OPEN. What was shown: how many orders (-1 when the order system would
        # not say) and how much work in flight. No model span may sit under it.
        required=frozenset({TENANT, "agent.opening.orders", "agent.opening.in_flight"}),
    ),
    "agent.portal.refused": SpanSpec(
        # A login that did not complete has no verified session to record. The
        # detail is why, for the operator; the customer saw a fixed sentence.
        required=frozenset(),
        optional=frozenset({"http.refusal_detail"}),
    ),
    "agent.escalation.resolve": SpanSpec(
        # The outcome is required. It is the ground truth behind the
        # over-escalation rate, and a close that did not record one is a close
        # that taught us nothing — which is how the false-positive rate stays
        # unmeasurable forever.
        required=frozenset({ESCALATION_ID, ESCALATION_RULE, "agent.escalation.outcome"}),
        optional=frozenset({"agent.escalation.waited_s"}),
    ),
    "gen_ai.chat": SpanSpec(
        # The current name is required; the deprecated one is merely allowed, so
        # dropping it later is a deletion rather than a contract change.
        required=frozenset({GEN_AI_PROVIDER}),
        optional=frozenset(
            {
                GEN_AI_SYSTEM,
                GEN_AI_OPERATION,
                GEN_AI_REQUEST_MODEL,
                "gen_ai.request.temperature",  # what was sent, not what was configured (F-064)
                GEN_AI_RESPONSE_MODEL,
                GEN_AI_INPUT_TOKENS,
                GEN_AI_OUTPUT_TOKENS,
                RESOLUTION,
                "agent.cassette.match",
                "prompt",
                "response",
            }
        ),
    ),
    "agent.tool": SpanSpec(
        required=frozenset({GEN_AI_TOOL_NAME, SIDE_EFFECT, IDEMPOTENCY_KEY}),
        optional=frozenset(
            {
                "agent.tool.replayed",
                "agent.tool.truncated",
                TOOL_OUTCOME,
                TOOL_ARGUMENTS,
                TOOL_RESULT,
            }
        ),
    ),
    "agent.tool.local": SpanSpec(required=frozenset({GEN_AI_TOOL_NAME})),
    # A customer's verdict on a conversation, recorded against it (AHC-0112).
    "agent.feedback": SpanSpec(required=frozenset({SESSION_ID, USER_ID, FEEDBACK})),
    "agent.tools.list": SpanSpec(required=frozenset({"agent.tools.count", "agent.tools.rejected"})),
    "agent.policy": SpanSpec(
        required=frozenset({"agent.policy.position"}),
        optional=frozenset({"agent.policy.blocked_by", "agent.policy.errored"}),
    ),
    # What an approver sees when they look (T-059). The reader's login, not a
    # tenant — the same distinction `agent.escalation.queue` makes.
    "agent.approval.queue": SpanSpec(
        required=frozenset({USER_ID}), optional=frozenset({"agent.approval.depth"})
    ),
    "agent.approval.request": SpanSpec(
        required=frozenset({"agent.approval.id", "agent.approval.action"})
    ),
    "agent.approval.decide": SpanSpec(
        required=frozenset({"agent.approval.id", "agent.approval.granted"})
    ),
    "agent.approval.resume": SpanSpec(required=frozenset({"agent.approval.id"})),
    # Granted by the policy and carried out in the same call — a refund that is
    # owed and within the limit (F-014). The decide span inside it names no person.
    "agent.approval.carry_out": SpanSpec(required=frozenset({"agent.approval.id"})),
    "agent.flow.fanout": SpanSpec(
        required=frozenset({"agent.flow.count"}), optional=frozenset({"agent.flow.peak"})
    ),
    "agent.flow.throttled": SpanSpec(required=frozenset({"agent.flow.delay_s"})),
    # AHC-0106. Emitted only when a completed reply promised something nothing
    # was doing — so a rate here is the rate at which the model writes cheques
    # this agent cannot cash, and `kind` says which cheque.
    "agent.promise.unbacked": SpanSpec(
        required=frozenset({"agent.promise.kind", "agent.promise.rules_version"})
    ),
    # AHC-0107. Emitted only when an irreversible action was about to run on a
    # belief older than its window, so a rate here says how often the world
    # moves faster than the conversation.
    "agent.freshness.refresh": SpanSpec(required=frozenset({"agent.freshness.rows"})),
    "agent.breaker": SpanSpec(required=frozenset({"agent.breaker.state"})),
    "agent.llm.retry": SpanSpec(required=frozenset({"agent.retry.attempt", "agent.retry.reason"})),
}


def validate(spans: Iterable[ReadableSpan]) -> list[str]:
    """Every way this trace departs from the contract.

    Returns violations rather than raising: a caller decides whether an
    incomplete trace fails a test or merely reports, and the eval harness wants
    the list rather than the first one.
    """
    out: list[str] = []
    for span in spans:
        # The contract governs this agent's spans. A library that instruments
        # itself (the MCP SDK emits `tools/list`) is not ours to hold to it.
        scope = span.instrumentation_scope
        if scope is not None and scope.name != TRACER_NAME:
            continue
        spec = CONTRACT.get(span.name)
        if spec is None:
            out.append(f"{span.name}: not in the span contract")
            continue
        out.extend(spec.violations(span.name, attributes_of(span)))
    return out


def attributes_of(finished: ReadableSpan) -> Mapping[str, Any]:
    """Read a finished span's attributes. Test-facing: this is the surface M5
    assertions use."""
    return dict(finished.attributes or {})
