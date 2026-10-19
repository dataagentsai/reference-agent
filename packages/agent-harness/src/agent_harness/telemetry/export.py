"""Sending spans to a collector, alongside the in-memory exporter (`export_to`)."""

from __future__ import annotations

from collections.abc import Mapping


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
    from agent_harness import telemetry as tel

    if tel._PROVIDER is None:  # noqa: SLF001 — the package's own provider
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(endpoint=endpoint, headers=dict(headers or {}))
    tel._PROVIDER.add_span_processor(BatchSpanProcessor(exporter))
    return True


__all__ = ["export_to"]
