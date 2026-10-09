"""The `telemetry` port: where the harness's spans and numbers go.

Every adapter installs the same thing first — our provider with the in-memory
exporter the span contract and the evals read — and differs only in what it
sends beyond this process:

    console             nothing: spans stay in memory (a Mac, a test)
    otel-to-langfuse    an OTLP collector (the Open Stack's Langfuse behind it)
    azure-monitor-otel  Application Insights, through the Azure Monitor distro
                        building the providers with ours inside them

The product is the in-memory exporter, which is what a caller asserts against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_harness import telemetry as tel
from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring

COMMON = {
    "service": Setting(),
    "capture_payloads": Setting(default=False),
    "capture_sample": Setting(default=1.0),
}


def _configured(wiring: Wiring, **extra: Any) -> Any:
    settings = wiring.settings
    return tel.configure(
        capture_payloads=bool(settings.get("capture_payloads")),
        capture_sample=float(settings.get("capture_sample", 1.0)),
        service_name=settings.get("service"),
        environment=wiring.environment,
        **extra,
    )


@asynccontextmanager
async def _console(wiring: Wiring) -> AsyncIterator[Any]:
    yield _configured(wiring)


@asynccontextmanager
async def _otlp(wiring: Wiring) -> AsyncIterator[Any]:
    settings = wiring.settings
    exporter = _configured(wiring, metrics_endpoint=settings.get("metrics_endpoint"))
    pairs = (p.split("=", 1) for p in str(settings.get("headers") or "").split(",") if "=" in p)
    headers = {k.strip(): v.strip() for k, v in pairs}
    if not tel.export_to(str(settings["endpoint"]), headers=headers):
        raise OverlayRefused("otel-to-langfuse: the tracer provider was not installed")
    yield exporter


@asynccontextmanager
async def _azure_monitor(wiring: Wiring) -> AsyncIterator[Any]:
    from agent_harness.telemetry import azure

    connection = str(wiring.settings["connection_string"])

    def backend(resource: Any, processors: Any, reader: Any) -> Any:
        return azure.install(connection, resource, processors, reader)

    yield _configured(wiring, backend=backend)


CONSOLE = Adapter("telemetry", "console", _console, COMMON)
OTLP = Adapter(
    "telemetry",
    "otel-to-langfuse",
    _otlp,
    {
        **COMMON,
        "endpoint": Setting(required=True),
        "headers": Setting(secret=True),
        "metrics_endpoint": Setting(),
    },
)
AZURE_MONITOR = Adapter(
    "telemetry",
    "azure-monitor-otel",
    _azure_monitor,
    {**COMMON, "connection_string": Setting(required=True, secret=True)},
)

__all__ = ["AZURE_MONITOR", "CONSOLE", "OTLP"]
