"""Metrics: a meter provider of our own, and the numbers derived from spans (T-055).

Beside the tracer provider in `telemetry`, for the same reason it is owned
rather than global: a set-once global cannot be reconfigured, and a test could
never read back what a turn recorded. `configure` rebinds every instrument in
`counters` to the new provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

from agent_harness.telemetry import counters
from agent_harness.telemetry.names import COST_USD, TERMINATION, TRACER_NAME

_METERS: MeterProvider | None = None
_METRIC_READER: InMemoryMetricReader | None = None
_EXPORTING_METRICS = False


class RunNumbers(SpanProcessor):
    """Why each run stopped and what it cost, counted from the finished run span.

    The loop already records both on `agent.run` — the termination reason and
    the running spend, whose last value is the run's total — so the numbers are
    derived where the span ends rather than written a second time at each of
    the loop's four exits, where a fifth exit would be missed (T-055).

    Spend was recorded per model call until then, as the running total, so a
    forty-call run landed in the histogram forty times at forty sizes; this is
    once per unit of work, which is what AAC-0008 asks for.
    """

    def on_end(self, span: ReadableSpan) -> None:
        scope = span.instrumentation_scope
        if span.name != "agent.run" or (scope is not None and scope.name != TRACER_NAME):
            return
        attributes = span.attributes or {}
        reason = attributes.get(TERMINATION)
        if reason is not None:
            counters.terminations.add(1, {"reason": str(reason)})
        spent = attributes.get(COST_USD)
        if isinstance(spent, int | float) and spent > 0:
            counters.spend.record(float(spent))


def configure(
    resource: Resource,
    endpoint: str | None,
    headers: Mapping[str, str] | None,
    interval_s: float,
) -> None:
    """A meter provider of our own, as for spans, and the instruments rebound to it.

    An in-memory reader always, which is what a test reads back; a periodic
    OTLP reader when an endpoint is given (AHC-0111). The readers of a meter
    provider are fixed when it is built, so unlike spans the endpoint is an
    argument here rather than something added afterwards.
    """
    global _METERS, _METRIC_READER, _EXPORTING_METRICS
    if _METERS is not None:
        _METERS.shutdown()
    reader = InMemoryMetricReader()
    readers: list[MetricReader] = [reader]
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, headers=dict(headers or {})),
                export_interval_millis=interval_s * 1000,
            )
        )
    _EXPORTING_METRICS = len(readers) > 1
    _METERS = MeterProvider(resource=resource, metric_readers=readers)
    _METRIC_READER = reader
    metrics.set_meter_provider(_METERS)  # no-op after the first call; harmless
    counters.bind(_METERS.get_meter(counters.METER_NAME))


def exporting_metrics() -> bool:
    """Whether metrics leave the process — what a deployment must not start without."""
    return _EXPORTING_METRICS


def metric_points(name: str) -> list[tuple[dict[str, Any], Any]]:
    """Every point recorded under `name`, as (attributes, point). Test-facing:
    the metrics equivalent of `attributes_of`."""
    if _METRIC_READER is None:
        return []
    data = _METRIC_READER.get_metrics_data()
    out: list[tuple[dict[str, Any], Any]] = []
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    out.extend((dict(p.attributes or {}), p) for p in metric.data.data_points)
    return out


def flush_metrics() -> None:
    if _METERS is not None:
        _METERS.force_flush()


__all__ = ["RunNumbers", "configure", "exporting_metrics", "flush_metrics", "metric_points"]
