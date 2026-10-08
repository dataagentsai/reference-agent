"""Application Insights, through the Azure Monitor OpenTelemetry distro (T-099).

The Azure stack's telemetry binding (`azure-monitor-otel`): the same spans and
numbers, exported to Application Insights by Microsoft's distro rather than to
a collector by OTLP. A configuration of `telemetry.configure`, not a second
telemetry: the `agent.*` spans, the span contract and the in-memory exporter the
evals assert against are all unchanged.

**Taken only when the connection string is set.** Unset, `configure` builds its
providers exactly as before, and the distro is never imported — it is an
optional dependency (`agent-harness[azure]`), so a deployment on the Open Stack
does not carry it.

**The distro builds the providers; ours go inside them.** `configure_azure_monitor`
creates its own tracer and meter providers and installs them as OpenTelemetry's
globals; it does not accept a provider built elsewhere. So the processors
`configure` would have added — the in-memory exporter, the run numbers — are
handed to it, and the providers it installs are adopted as ours. Sampling is
pinned to every span: the distro's default rate-limits, and a sampled-out span
is one the span contract and the evals never see.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def connection_string(environ: Mapping[str, str] | None = None) -> str | None:
    """The Application Insights connection string, or None when this is not Azure."""
    value = (os.environ if environ is None else environ).get(CONNECTION_STRING, "").strip()
    return value or None


def install(
    connection: str,
    resource: Resource,
    processors: Sequence[SpanProcessor],
    reader: MetricReader,
) -> tuple[TracerProvider, MeterProvider]:
    """Run the distro with our resource, processors and reader; return what it built.

    Fails rather than adopting the wrong thing: OpenTelemetry's globals are set
    once, so a process that installed a provider before this ran would leave the
    distro's ignored and our spans going nowhere near Application Insights.
    """
    from azure.monitor.opentelemetry import configure_azure_monitor

    before = trace.get_tracer_provider(), metrics.get_meter_provider()
    configure_azure_monitor(
        connection_string=connection,
        resource=resource,
        span_processors=list(processors),
        metric_readers=[reader],
        sampling_ratio=1.0,
    )
    tracer_provider, meter_provider = trace.get_tracer_provider(), metrics.get_meter_provider()
    built = isinstance(tracer_provider, TracerProvider) and isinstance(
        meter_provider, MeterProvider
    )
    if not built or tracer_provider is before[0] or meter_provider is before[1]:
        raise RuntimeError(
            "Azure Monitor could not install its providers: OpenTelemetry's were "
            "already set in this process, so telemetry.configure must run first"
        )
    assert isinstance(tracer_provider, TracerProvider)
    assert isinstance(meter_provider, MeterProvider)
    return tracer_provider, meter_provider


__all__ = ["CONNECTION_STRING", "connection_string", "install"]
