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

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, StatusCode

from agent_harness.telemetry import azure, counters, meters, names
from agent_harness.telemetry.contract import (
    CONTRACT,
    SpanSpec,
    attributes_of,
    validate,
)
from agent_harness.telemetry.meters import exporting_metrics, flush_metrics, metric_points
from agent_harness.telemetry.names import (
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
    INPUT,
    ITERATION,
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
    TURN_RESULT,
    TURN_RULE,
    USER_ID,
    scope_name,
    service_name,
    set_identity,
)
from agent_harness.telemetry.redaction import (
    _REDACTIONS,
    redact,
)


def identify(*, scope: str, service: str) -> None:
    """Name the agent this telemetry is for: its instrumentation scope, which
    its spans and numbers carry and the span contract holds to account, and the
    `service.name` a provider is built with when `configure` is not told one.

    The agent's to say, once, before its first span — the library has no agent
    name of its own to fall back on, only a neutral one (`DEFAULT_SCOPE`).
    """
    set_identity(scope, service)
    meters.rebind()


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
_CAPTURE_SAMPLE = 1.0
_PROVIDER: TracerProvider | None = None
_CAPTURING: ContextVar[bool] = ContextVar("capturing", default=False)
"""Whether this turn's words are kept. Decided once per turn, so a turn is
captured whole or not at all: a sample whose tool results were kept and whose
reply was not is a sample no rule can read (AHC-0114)."""
_LAST_EXPORTER: InMemorySpanExporter | None = None
"""The most recently configured exporter, so a harness can check the span
contract without every test threading the exporter through."""


def configure(
    *,
    capture_payloads: bool = False,
    capture_sample: float = 1.0,
    service_name: str | None = None,
    service_version: str | None = None,
    environment: str | None = None,
    metrics_endpoint: str | None = None,
    metrics_headers: Mapping[str, str] | None = None,
    metrics_interval_s: float = 15.0,
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

    On the Azure stack — `APPLICATIONINSIGHTS_CONNECTION_STRING` set — the Azure
    Monitor distro builds both providers instead, with the same resource and the
    same processors inside them (`telemetry.azure`). Unset, nothing differs.
    """
    global _CAPTURE_PAYLOADS, _CAPTURE_SAMPLE, _PROVIDER, _LAST_EXPORTER
    _CAPTURE_PAYLOADS = capture_payloads
    _CAPTURE_SAMPLE = capture_sample
    exporter = InMemorySpanExporter()
    # `deployment.environment.name` is the current spelling; the older
    # `deployment.environment` is deprecated. Attributes are dropped when unset
    # rather than filled with "unknown", so an absent value stays absent.
    attributes = {"service.name": service_name or names.service_name()}
    if service_version is not None:
        attributes["service.version"] = service_version
    if environment is not None:
        attributes["deployment.environment.name"] = environment
    _PROVIDER = _install(
        Resource.create(attributes),
        (SimpleSpanProcessor(exporter), meters.RunNumbers()),
        metrics_endpoint,
        metrics_headers,
        metrics_interval_s,
    )
    _LAST_EXPORTER = exporter
    return exporter


def _install(
    resource: Resource,
    processors: tuple[SpanProcessor, ...],
    metrics_endpoint: str | None,
    metrics_headers: Mapping[str, str] | None,
    metrics_interval_s: float,
) -> TracerProvider:
    connection = azure.connection_string()
    if connection is not None:
        # The Azure stack: the distro builds both providers, exporting to
        # Application Insights, with ours inside them (`telemetry.azure`).
        reader = InMemoryMetricReader()
        provider, meter_provider = azure.install(connection, resource, processors, reader)
        meters.adopt(meter_provider, reader)
        return provider
    provider = TracerProvider(resource=resource)
    for processor in processors:
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)  # no-op after the first call; harmless
    meters.configure(resource, metrics_endpoint, metrics_headers, metrics_interval_s)
    return provider


def capture_decision(run_id: str) -> bool:
    """Whether a turn with this run id has its words kept.

    Deterministic in the run id rather than random, so the decision can be
    recomputed later from the record alone and a replay makes the same one.
    Pure: deciding does not switch anything on.
    """
    return _CAPTURE_PAYLOADS and _sampled(run_id, _CAPTURE_SAMPLE)


@contextmanager
def turn_scope(run_id: str, config: str | None = None) -> Iterator[bool]:
    """What telemetry holds for exactly one turn, and gives back when it ends.

    Two things: whether the turn's words are kept (F-084), and the configuration
    fingerprint its numbers are labelled with (`counters.configured`). The
    capture decision used to be set on the context and never taken back, so it
    outlived its turn: whatever ran next in the same task — an opening, the
    next caller's work, a test — kept or dropped words by a decision made for
    something else, and switching capture off did not withdraw it. Both are now
    restored on the way out, whatever way the turn leaves.
    """
    keep = capture_decision(run_id)
    token = _CAPTURING.set(keep)
    try:
        with counters.configured(config):
            yield keep
    finally:
        _CAPTURING.reset(token)


def capturing() -> bool:
    return _CAPTURING.get()


def _sampled(key: str, rate: float) -> bool:
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def export_to(endpoint: str, *, headers: Mapping[str, str] | None = None) -> bool:
    """Also send spans to a collector. Returns whether one was added.

    **Alongside, never instead.** The in-memory exporter stays: it is what the
    eval harness asserts against, it costs nothing, and a suite that stopped
    seeing spans the moment a deployment gained a backend would be a suite that
    only works where nobody is watching.

    `BatchSpanProcessor` here where the in-memory one is `Simple`: a network
    export on the request path would put a collector's latency inside a
    customer's turn, and a collector that is down would put its failure there.
    Batching is the whole reason the processor interface is separate from the
    exporter.

    **Nothing needs renaming to make this useful.** The names this agent emits
    are the OTel GenAI semantic conventions — `gen_ai.usage.input_tokens`,
    `session.id`, `user.id` — so a backend maps them on ingest without a
    translation layer here. The `agent.*` family will arrive as opaque
    attributes, because no backend knows what an idempotency key or a route kind
    is; that is a property of the ecosystem and not a gap to paper over by
    renaming them into somebody's vocabulary.

    No-ops when the provider has not been configured, so a caller need not
    order the two.
    """
    if _PROVIDER is None:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(endpoint=endpoint, headers=dict(headers or {}))
    _PROVIDER.add_span_processor(BatchSpanProcessor(exporter))
    return True


def _tracer() -> trace.Tracer:
    if _PROVIDER is not None:
        return _PROVIDER.get_tracer(scope_name())
    return trace.get_tracer(scope_name())


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


def set_payload(current: Span, key: str, text: str, *, limit: int = 4000) -> None:
    """Capture words — redacted, bounded, and only for a turn chosen for capture.

    Off by default. Payload capture is the single largest privacy surface in an
    agent, and a default that leaks is a default that ships. When on, it is on
    for a declared sample of turns (`turn_scope`), not for all traffic.

    Until T-057 this was defined and called from nowhere, so switching
    capture on captured nothing (found writing AHC-0114).
    """
    # Both: inside a turn chosen for capture, and capture still on (F-084).
    if _CAPTURE_PAYLOADS and _CAPTURING.get():
        current.set_attribute(key, redact(text)[:limit])


def set_usage(current: Span, *, input_tokens: int, output_tokens: int) -> None:
    current.set_attribute(GEN_AI_INPUT_TOKENS, input_tokens)
    current.set_attribute(GEN_AI_OUTPUT_TOKENS, output_tokens)


__all__ = [
    "CAPTURED",
    "FEEDBACK",
    "INPUT",
    "REPLY",
    "REPLY_REDACTED",
    "SYNTHETIC",
    "TOOL_ARGUMENTS",
    "TOOL_OUTCOME",
    "TOOL_RESULT",
    "TURN_RESULT",
    "TURN_RULE",
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
    "counters",
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
    "TOOL_CALL_BOUND",
    "USER_ID",
    "_REDACTIONS",
    "attributes_of",
    "configure",
    "identify",
    "scope_name",
    "service_name",
    "redact",
    "set_current_attribute",
    "capture_decision",
    "turn_scope",
    "capturing",
    "exporting_metrics",
    "flush_metrics",
    "metric_points",
    "set_payload",
    "set_usage",
    "span",
    "validate",
]
