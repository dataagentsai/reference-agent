"""T-099 — the agent names its telemetry; Azure Monitor is a configuration of it.

Two tables. The first holds the names: the library has none of its own, the
support agent sets the values the library used to hard-code, and a trace built
under them looks as it always did. The second holds the Azure branch: taken only
when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, with the distro's
`configure_azure_monitor` replaced by a fake that builds providers the way the
distro does — no network, no Application Insights.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import azure.monitor.opentelemetry as distro
import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

import support_agent  # noqa: F401  the package root names the agent's telemetry
from agent_harness import telemetry as tel
from agent_harness.telemetry import azure, counters, names
from support_agent.telemetry import spans

CONNECTION = "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://x"


@pytest.fixture(autouse=True)
def _restore_identity() -> Iterator[None]:
    yield
    tel.identify(scope=spans.SCOPE, service=spans.SERVICE)
    tel.configure()


def _one_run(exporter: Any) -> tuple[str, str]:
    """Emit one of this agent's spans and read back its scope and service names."""
    exporter.clear()
    with tel.span("agent.direct", **{"agent.handler": "order_status"}):
        pass
    (finished,) = exporter.get_finished_spans()
    scope = finished.instrumentation_scope
    return (scope.name if scope else ""), str(finished.resource.attributes["service.name"])


# (name, identity to set or None for the agent's own, configure's service_name,
#  expected scope on the span, expected service.name)
NAMES: list[tuple[str, tuple[str, str] | None, str | None, str, str]] = [
    ("the support agent's names, as before", None, None, "support_agent", "support-agent"),
    (
        "an explicit service name wins",
        None,
        "support-agent-canary",
        "support_agent",
        "support-agent-canary",
    ),
    (
        "a second agent names its own",
        ("claims_agent", "claims-agent"),
        None,
        "claims_agent",
        "claims-agent",
    ),
]


@pytest.mark.parametrize(
    ("name", "identity", "service", "scope", "service_name"), NAMES, ids=[row[0] for row in NAMES]
)
def test_names_are_the_agents(
    name: str,
    identity: tuple[str, str] | None,
    service: str | None,
    scope: str,
    service_name: str,
) -> None:
    if identity is not None:
        tel.identify(scope=identity[0], service=identity[1])
    exporter = tel.configure(service_name=service)
    assert _one_run(exporter) == (scope, service_name)
    assert tel.scope_name() == scope
    # The span contract holds exactly this scope to account.
    assert tel.validate(exporter.get_finished_spans()) == []


def test_the_library_has_no_agent_name() -> None:
    assert (names.DEFAULT_SCOPE, names.DEFAULT_SERVICE) == ("agent_harness", "agent")
    assert "support" not in names.DEFAULT_SCOPE + names.DEFAULT_SERVICE


@pytest.mark.parametrize(("scope", "service"), [("", "x"), ("x", "")])
def test_an_empty_name_is_refused(scope: str, service: str) -> None:
    with pytest.raises(ValueError):
        tel.identify(scope=scope, service=service)


class FakeDistro:
    """Does what `configure_azure_monitor` does with providers — builds both from
    the arguments and installs them as the globals — and records the call."""

    def __init__(self, installs: bool = True) -> None:
        self.installs = installs
        self.calls: list[dict[str, Any]] = []
        self.tracer: Any = trace.ProxyTracerProvider()
        self.meter: Any = metrics.NoOpMeterProvider()

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if not self.installs:
            return
        self.tracer = TracerProvider(resource=kwargs["resource"])
        for processor in kwargs["span_processors"]:
            self.tracer.add_span_processor(processor)
        self.meter = MeterProvider(
            resource=kwargs["resource"], metric_readers=kwargs["metric_readers"]
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDistro:
    installed = FakeDistro()
    monkeypatch.setattr(distro, "configure_azure_monitor", installed)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: installed.tracer)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: installed.meter)
    return installed


# (name, APPLICATIONINSIGHTS_CONNECTION_STRING or None for unset, distro called)
BRANCHES: list[tuple[str, str | None, bool]] = [
    ("unset: the providers are built as before", None, False),
    ("empty: treated as unset", "", False),
    ("blank: treated as unset", "   ", False),
    ("set: the distro builds the providers, ours inside them", CONNECTION, True),
]


@pytest.mark.parametrize(("name", "value", "called"), BRANCHES, ids=[r[0] for r in BRANCHES])
def test_azure_monitor_only_when_connection_string_set(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeDistro,
    name: str,
    value: str | None,
    called: bool,
) -> None:
    if value is None:
        monkeypatch.delenv(azure.CONNECTION_STRING, raising=False)
    else:
        monkeypatch.setenv(azure.CONNECTION_STRING, value)
    exporter = tel.configure(environment="dev")

    assert len(fake.calls) == int(called)
    # Whichever way it was built, the agent.* spans are the same and still seen.
    assert _one_run(exporter) == ("support_agent", "support-agent")
    assert tel.validate(exporter.get_finished_spans()) == []
    counters.turns.add(1, {"route": "probe"})
    assert tel.metric_points("agent.turns")
    assert tel.exporting_metrics() is called
    if called:
        (kwargs,) = fake.calls
        assert kwargs["connection_string"] == CONNECTION
        assert kwargs["sampling_ratio"] == 1.0  # every span, or the contract goes blind
        assert kwargs["resource"].attributes["service.name"] == "support-agent"
        assert kwargs["resource"].attributes["deployment.environment.name"] == "dev"
        assert tel._PROVIDER is fake.tracer


def test_a_distro_that_could_not_install_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, fake: FakeDistro
) -> None:
    """OpenTelemetry's globals are set once: a distro whose providers were ignored
    would leave spans going nowhere near Application Insights, silently."""
    fake.installs = False
    monkeypatch.setenv(azure.CONNECTION_STRING, CONNECTION)
    with pytest.raises(RuntimeError, match="already set"):
        tel.configure()
