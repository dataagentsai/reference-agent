"""Whether a past run can be explained.

L11 · P3. OpenTelemetry on the wire, GenAI semantic conventions for attribute
names. OTel-native is what keeps the backend a configuration line rather than a
dependency — Langfuse, Phoenix or nothing at all, without touching this module.

Two things here are not decoration:

**One redaction point.** Every payload that reaches a span passes through
`redact`. AAC-0095 requires logged prompts and responses to be redacted and
retention-bounded, and a redaction rule applied in six places is applied in five.

**An in-memory exporter for tests.** AAC's M5 — trace assertion — is the only
mechanism that tests *how* a system reached an answer rather than what it said.
Making it available with no backend is what turns M5 from an aspiration into a
pytest assertion that runs in milliseconds.

Sibling of `config` and may not import it. Values come in from the composition
root.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, StatusCode

# --------------------------------------------------------------------------- #
# GenAI semantic conventions. Kept in one place because the specification is
# still moving; changing a name here must not mean grepping the codebase.
# --------------------------------------------------------------------------- #

GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"

# Ours. Namespaced so they are visibly not part of the standard.
RUN_ID = "agent.run.id"
STEP = "agent.step"
ITERATION = "agent.iteration"
CONFIG_FINGERPRINT = "agent.config.fingerprint"
ROUTE_KIND = "agent.route.kind"
ROUTE_REASON = "agent.route.reason"
"""AAC-0100 — the serving route is recorded, with its reason and its cost."""
COST_USD = "agent.cost.usd"
COST_CALL_USD = "agent.cost.call_usd"
TENANT = "agent.tenant"
"""AAC-0104 — spend is attributable to tenant, feature and route. Tenant here,
feature is the handler or intent, route is ROUTE_KIND above."""
IDEMPOTENCY_KEY = "agent.idempotency.key"
SIDE_EFFECT = "agent.tool.side_effect"
MODEL_MALFORMED = "agent.model.malformed"
"""How many provider responses could not be parsed this run — AHC-0001.

An attribute rather than a log line: a parse failure rate is a number that moves
when a model is swapped, and one that only exists in logs is one nobody plots."""

TERMINATION = "agent.termination.reason"
RESOLUTION = "agent.resolution"
"""mock | replay | real | shadow. A verdict is not interpretable without it."""

_TRACER_NAME = "support_agent"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{13,19}\b"), "[card]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b(?:\+91[- ]?)?[6-9]\d{9}\b"), "[phone]"),
    (re.compile(r"(?i)\b(sk|gsk|key)[-_][A-Za-z0-9]{8,}"), "[secret]"),
)


# --------------------------------------------------------------------------- #
# The span contract.
#
# AHC-0011 says every call emits a **complete** trace. Complete against what?
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
        required=frozenset({RUN_ID}),
        optional=frozenset({CONFIG_FINGERPRINT, RESOLUTION}),
    ),
    "agent.run": SpanSpec(
        required=frozenset({RUN_ID, TENANT}),
        optional=frozenset(
            {TERMINATION, COST_USD, COST_CALL_USD, MODEL_MALFORMED, "agent.policy.blocked_by"}
        ),
    ),
    "agent.step": SpanSpec(required=frozenset({STEP, RUN_ID})),
    "agent.route": SpanSpec(
        required=frozenset({ROUTE_KIND, ROUTE_REASON, "agent.router.rules_version"})
    ),
    "agent.direct": SpanSpec(required=frozenset({"agent.handler"})),
    "gen_ai.chat": SpanSpec(
        required=frozenset({GEN_AI_SYSTEM}),
        optional=frozenset(
            {
                GEN_AI_OPERATION,
                GEN_AI_REQUEST_MODEL,
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
        optional=frozenset({"agent.tool.replayed", "agent.tool.truncated"}),
    ),
    "agent.tool.local": SpanSpec(required=frozenset({GEN_AI_TOOL_NAME})),
    "agent.tools.list": SpanSpec(required=frozenset({"agent.tools.count", "agent.tools.rejected"})),
    "agent.policy": SpanSpec(
        required=frozenset({"agent.policy.position"}),
        optional=frozenset({"agent.policy.blocked_by", "agent.policy.errored"}),
    ),
    "agent.approval.request": SpanSpec(
        required=frozenset({"agent.approval.id", "agent.approval.action"})
    ),
    "agent.approval.decide": SpanSpec(
        required=frozenset({"agent.approval.id", "agent.approval.granted"})
    ),
    "agent.approval.resume": SpanSpec(required=frozenset({"agent.approval.id"})),
    "agent.flow.fanout": SpanSpec(
        required=frozenset({"agent.flow.count"}), optional=frozenset({"agent.flow.peak"})
    ),
    "agent.flow.throttled": SpanSpec(required=frozenset({"agent.flow.delay_s"})),
    "agent.breaker": SpanSpec(required=frozenset({"agent.breaker.state"})),
}


def validate(spans: Iterable[ReadableSpan]) -> list[str]:
    """Every way this trace departs from the contract.

    Returns violations rather than raising: a caller decides whether an
    incomplete trace fails a test or merely reports, and the eval harness wants
    the list rather than the first one.
    """
    out: list[str] = []
    for span in spans:
        spec = CONTRACT.get(span.name)
        if spec is None:
            out.append(f"{span.name}: not in the span contract")
            continue
        out.extend(spec.violations(span.name, attributes_of(span)))
    return out


def redact(text: str, *, limit: int = 4000) -> str:
    """The single redaction point. Everything captured onto a span comes here.

    Deliberately crude: deterministic regexes, no model call, no network. A
    detector that can be wrong slowly is worse than one that is obviously
    approximate — this is a floor, not a compliance control.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return text[:limit] + f"…[truncated {len(text) - limit} chars]"
    return text


_CAPTURE_PAYLOADS = False
_PROVIDER: TracerProvider | None = None
_LAST_EXPORTER: InMemorySpanExporter | None = None
"""The most recently configured exporter, so a harness can check the span
contract without every test threading the exporter through."""


def configure(*, capture_payloads: bool = False) -> InMemorySpanExporter:
    """Install a provider and return the in-memory exporter.

    We hold our own provider rather than relying on OpenTelemetry's global,
    which is set-once: a second `set_tracer_provider` is ignored with a warning,
    so a module built on the global cannot be reconfigured — and a module that
    cannot be reconfigured cannot be tested. The global is still set on a
    best-effort basis so third-party instrumentation lands in the same trace.

    In production the composition root adds an OTLP processor alongside. The
    in-memory exporter stays regardless: it costs nothing, and it is what the
    eval harness asserts against.
    """
    global _CAPTURE_PAYLOADS, _PROVIDER
    _CAPTURE_PAYLOADS = capture_payloads
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _PROVIDER = provider
    global _LAST_EXPORTER
    _LAST_EXPORTER = exporter
    trace.set_tracer_provider(provider)  # no-op after the first call; harmless
    return exporter


def _tracer() -> trace.Tracer:
    if _PROVIDER is not None:
        return _PROVIDER.get_tracer(_TRACER_NAME)
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """A span with attributes set at open, and errors recorded rather than
    swallowed. Exceptions propagate — observability never changes control flow."""
    with _tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.set_status(StatusCode.ERROR, str(exc))
            current.record_exception(exc)
            raise


def set_payload(current: Span, key: str, text: str) -> None:
    """Capture a prompt or response — redacted, and only when enabled.

    Off by default. Payload capture is the single largest privacy surface in an
    agent, and a default that leaks is a default that ships.
    """
    if _CAPTURE_PAYLOADS:
        current.set_attribute(key, redact(text))


def set_usage(current: Span, *, input_tokens: int, output_tokens: int) -> None:
    current.set_attribute(GEN_AI_INPUT_TOKENS, input_tokens)
    current.set_attribute(GEN_AI_OUTPUT_TOKENS, output_tokens)


def attributes_of(finished: ReadableSpan) -> Mapping[str, Any]:
    """Read a finished span's attributes. Test-facing: this is the surface M5
    assertions use."""
    return dict(finished.attributes or {})


__all__ = [
    "CONFIG_FINGERPRINT",
    "CONTRACT",
    "COST_CALL_USD",
    "COST_USD",
    "GEN_AI_INPUT_TOKENS",
    "GEN_AI_OPERATION",
    "GEN_AI_OUTPUT_TOKENS",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_RESPONSE_MODEL",
    "GEN_AI_SYSTEM",
    "GEN_AI_TOOL_NAME",
    "IDEMPOTENCY_KEY",
    "ITERATION",
    "RESOLUTION",
    "ROUTE_KIND",
    "ROUTE_REASON",
    "RUN_ID",
    "SIDE_EFFECT",
    "STEP",
    "TENANT",
    "TERMINATION",
    "SpanSpec",
    "attributes_of",
    "configure",
    "redact",
    "set_payload",
    "set_usage",
    "span",
    "validate",
]
