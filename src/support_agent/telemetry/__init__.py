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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, StatusCode

from support_agent.telemetry.contract import (
    CONTRACT,
    SpanSpec,
    attributes_of,
    validate,
)
from support_agent.telemetry.names import (
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
    FRESHNESS_ROWS,
    FRESHNESS_UNCHECKABLE,
    GEN_AI_INPUT_TOKENS,
    GEN_AI_OPERATION,
    GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_NAME,
    IDEMPOTENCY_KEY,
    ITERATION,
    MODEL_MALFORMED,
    RESOLUTION,
    ROUTE_KIND,
    ROUTE_REASON,
    RUN_ID,
    SESSION_ID,
    SIDE_EFFECT,
    STEP,
    TENANT,
    TERMINATION,
    TRACER_NAME,
    USER_ID,
)
from support_agent.telemetry.redaction import (
    _REDACTIONS,
    redact,
)

_TRACER_NAME = TRACER_NAME


def set_current_attribute(name: str, value: Any) -> None:
    """Record onto whatever span is already open.

    For the case where the fact is learned in a helper several calls below the
    span that should carry it, and threading the span down would mean four
    signatures growing a parameter to serve one attribute. A no-op when nothing
    is open, which is the honest behaviour for a caller that may run outside a
    turn.
    """
    trace.get_current_span().set_attribute(name, value)


_CAPTURE_PAYLOADS = False
_PROVIDER: TracerProvider | None = None
_LAST_EXPORTER: InMemorySpanExporter | None = None
"""The most recently configured exporter, so a harness can check the span
contract without every test threading the exporter through."""


def configure(
    *,
    capture_payloads: bool = False,
    service_name: str = "support-agent",
    service_version: str | None = None,
    environment: str | None = None,
) -> InMemorySpanExporter:
    """Install a provider and return the in-memory exporter.

    The resource is set here rather than left to default. Every backend groups
    and bills by `service.name`, and a provider built without one reports
    `unknown_service` — which is not a cosmetic problem: it is a whole
    deployment's telemetry landing in a bucket that cannot be told apart from
    anyone else's.

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
    # `deployment.environment.name` is the current spelling; the older
    # `deployment.environment` is deprecated. Attributes are dropped when unset
    # rather than filled with "unknown", so an absent value stays absent.
    attributes = {"service.name": service_name}
    if service_version is not None:
        attributes["service.version"] = service_version
    if environment is not None:
        attributes["deployment.environment.name"] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
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


__all__ = [
    "CONFIG_FINGERPRINT",
    "CONTEXT_CHARS",
    "CONTEXT_EXCHANGES",
    "CONTEXT_STORED",
    "CONTEXT_TRIMMED",
    "CONTRACT",
    "COST_CALL_USD",
    "COST_USD",
    "ESCALATION_ID",
    "ESCALATION_RULE",
    "ESCALATION_TIER",
    "GEN_AI_INPUT_TOKENS",
    "GEN_AI_OPERATION",
    "GEN_AI_OUTPUT_TOKENS",
    "GEN_AI_PROVIDER",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_RESPONSE_MODEL",
    "GEN_AI_SYSTEM",
    "GEN_AI_TOOL_NAME",
    "IDEMPOTENCY_KEY",
    "ITERATION",
    "FRESHNESS_ROWS",
    "FRESHNESS_UNCHECKABLE",
    "MODEL_MALFORMED",
    "RESOLUTION",
    "ROUTE_KIND",
    "ROUTE_REASON",
    "RUN_ID",
    "SESSION_ID",
    "SIDE_EFFECT",
    "STEP",
    "SpanSpec",
    "TENANT",
    "TERMINATION",
    "USER_ID",
    "_REDACTIONS",
    "attributes_of",
    "configure",
    "redact",
    "set_current_attribute",
    "set_payload",
    "set_usage",
    "span",
    "validate",
]
