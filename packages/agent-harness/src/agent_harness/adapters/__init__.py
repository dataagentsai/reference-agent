"""The composition, from configuration: a profile, an overlay, and the registry.

Inversion of control, made the only way to wire an agent. Mechanism depends on
ports (`contracts.protocols`); an adapter realises one; and which adapter fills
each port is read from YAML at one composition root, never from an
`if env == ...` branch. So moving to another cloud, model layer or check
provider is a new adapter module, a line in an overlay, and that adapter passing
its port's contract tests — and nothing else.

    profile    harness-profile.yaml, `extends` resolved (`config.profile`): the
               adapter the stack binds each port to — what production runs.
    overlay    config/<environment>.yaml: which adapter each port gets *here*,
               its settings, and secrets by reference (`{env: NAME}`,
               `{key_vault: name}`), never by value. An adapter that differs
               from the profile's must say why, the way a profile leaving its
               stack must (`x_why`).
    registry   `ADAPTERS`: port -> adapter name -> `module:attribute`, lazily,
               under the names the stack profiles use.

`plan` reads and checks all three and builds nothing, so an overlay that cannot
run yet (Azure before Tier 4) is still proved to resolve. `compose` builds a
plan's adapters in dependency order, each inside the exit stack of the caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_harness.config import profile as profiles
from agent_harness.config.registry import Registry
from agent_harness.contracts.failures import AgentFailure, Fault

PORTS = (
    "secrets",
    "telemetry",
    "model",
    "state",
    "records",
    "identity",
    "tool_runtime",
    "approval",
)
"""The ports composed from configuration, in the order they are built: each may
read the ones before it (secrets first, so every other setting can be a reference).
The secrets port's own settings can only name the environment (`{env: NAME}`):
nothing that reads a vault exists yet when the vault's address is read."""

ADAPTERS = Registry(
    "port",
    {
        "secrets": {
            "environment-settings": "agent_harness.adapters.secrets:ENVIRONMENT",
            "key-vault": "agent_harness.adapters.secrets:KEY_VAULT",
        },
        "telemetry": {
            "console": "agent_harness.adapters.telemetry:CONSOLE",
            "otel-to-langfuse": "agent_harness.adapters.telemetry:OTLP",
            "azure-monitor-otel": "agent_harness.adapters.telemetry:AZURE_MONITOR",
        },
        "model": {
            "scripted": "agent_harness.adapters.model:SCRIPTED",
            "litellm-proxy": "agent_harness.adapters.model:LITELLM_PROXY",
            "groq-direct": "agent_harness.adapters.model:GROQ_DIRECT",
            "apim-ai-gateway": "agent_harness.adapters.model:APIM",
        },
        "state": {
            "in-memory": "agent_harness.adapters.state:IN_MEMORY",
            "postgres": "agent_harness.adapters.state:POSTGRES",
            "azure-postgresql-flexible": "agent_harness.adapters.state:AZURE_POSTGRES",
        },
        "records": {
            "in-memory": "agent_harness.adapters.records:IN_MEMORY",
            "postgres": "agent_harness.adapters.records:POSTGRES",
        },
        "identity": {
            "local-dev": "agent_harness.adapters.identity:LOCAL_DEV",
            "keycloak": "agent_harness.adapters.identity:KEYCLOAK",
            "entra-id": "agent_harness.adapters.identity:ENTRA_ID",
        },
        "tool_runtime": {"mcp-client": "agent_harness.adapters.tools:MCP_CLIENT"},
        "approval": {
            "temporal-updates": "agent_harness.adapters.waits:TEMPORAL",
            "dbos-workflows": "agent_harness.adapters.waits:DBOS",
        },
    },
)
"""Every adapter the library ships, under the name `stacks/*.yaml` binds it by.
Names no stack used yet were added: `scripted`, `groq-direct`, `in-memory`,
`local-dev`, `console`. An agent adds its own with `ADAPTERS.merged(...)`."""


class OverlayRefused(AgentFailure):
    """The overlay, or the profile it names, does not compose; nothing starts."""

    fault = Fault.MISCONFIGURED


@dataclass(frozen=True)
class Setting:
    """One setting an adapter reads from its overlay entry."""

    required: bool = False
    default: Any = None
    secret: bool = False
    """Only by reference (`{env: …}` or `{key_vault: …}`): a value is refused."""


@dataclass(frozen=True)
class Wiring:
    """What an adapter is built from: its settings (references resolved), the
    products of the ports built before it, and the composition root's hooks."""

    settings: Mapping[str, Any]
    built: Mapping[str, Any]
    hooks: Mapping[str, Any]
    environment: str
    base: Path = Path(".")
    """The overlay's folder: a relative path in a setting is relative to it."""


Build = Callable[[Wiring], AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class Adapter:
    port: str
    name: str
    build: Build
    settings: Mapping[str, Setting] = field(default_factory=dict)
    hooks: tuple[str, ...] = ()
    """What the composition root must hand in: an agent's own work (a payout's
    steps), its resolved model choice, a script. Missing ones fail at compose."""


@dataclass(frozen=True)
class Bound:
    adapter: Adapter
    settings: Mapping[str, Any]
    why: str = ""


@dataclass(frozen=True)
class Plan:
    environment: str
    bound: Mapping[str, Bound]
    app: Mapping[str, Any]
    profile: Mapping[str, Any]
    base: Path = Path(".")

    def adapter(self, port: str) -> str:
        return self.bound[port].adapter.name


def is_reference(value: object) -> bool:
    return isinstance(value, Mapping) and bool({"env", "key_vault"} & set(value))


def plan(overlay: Path, *, registry: Registry = ADAPTERS) -> Plan:
    """Read the overlay and its profile, look every adapter up, check every
    setting. Imports each chosen adapter's factory module, never its SDK."""
    document = _yaml(overlay)
    _only(document, {"apiVersion", "environment", "profile", "bindings", "app"}, overlay.name)
    if "profile" not in document or "environment" not in document:
        raise OverlayRefused(f"{overlay.name}: names no `profile` or no `environment`")
    try:
        resolved = profiles.resolve((overlay.parent / document["profile"]).resolve())
    except (FileNotFoundError, ValueError) as exc:
        raise OverlayRefused(f"{overlay.name}: {exc}") from None
    stack = resolved.get("bindings") or {}
    bindings = document.get("bindings") or {}
    _only(bindings, set(registry.slots()), f"{overlay.name}: bindings")
    bound = {
        port: _bound(port, entry, registry, stack.get(port) or {}, overlay.name)
        for port, entry in bindings.items()
    }
    app = dict(document.get("app") or {})
    return Plan(str(document["environment"]), bound, app, resolved, overlay.parent.resolve())


def _bound(
    port: str, entry: Any, registry: Registry, stack: Mapping[str, Any], where: str
) -> Bound:
    if not isinstance(entry, Mapping) or "adapter" not in entry:
        raise OverlayRefused(f"{where}: bindings.{port} names no `adapter`")
    adapter: Adapter = registry.load(port, str(entry["adapter"]))
    _only(entry, {"adapter", "why", *adapter.settings}, f"{where}: bindings.{port}")
    if stack.get("adapter") not in (None, adapter.name) and not entry.get("why"):
        raise OverlayRefused(
            f"{where}: bindings.{port} is {adapter.name}, the profile binds "
            f"{stack['adapter']}: say `why` it differs here"
        )
    settings = {k: v for k, v in entry.items() if k not in ("adapter", "why")}
    for name, setting in adapter.settings.items():
        value = settings.get(name, setting.default)
        if setting.required and value is None:
            raise OverlayRefused(f"{where}: bindings.{port}.{name} is required by {adapter.name}")
        if setting.secret and value is not None and not is_reference(value):
            raise OverlayRefused(
                f"{where}: bindings.{port}.{name} is a secret: give a reference "
                "({env: NAME} or {key_vault: name}), never the value"
            )
        settings[name] = value
    return Bound(adapter, settings, str(entry.get("why", "")))


def _yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OverlayRefused(f"no overlay at {path}")
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise OverlayRefused(f"{path.name}: expected a mapping")
    return loaded


def _only(found: Mapping[str, Any], allowed: set[str], where: str) -> None:
    if not isinstance(found, Mapping):
        raise OverlayRefused(f"{where}: expected a mapping")
    unknown = sorted(set(found) - allowed)
    if unknown:
        raise OverlayRefused(
            f"{where}: unknown field {unknown[0]!r}; allowed: {', '.join(sorted(allowed))}"
        )


@asynccontextmanager
async def compose(
    planned: Plan,
    *,
    hooks: Mapping[str, Mapping[str, Any]] | None = None,
    ports: tuple[str, ...] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Build `ports` (default: every port the overlay binds), in `PORTS` order,
    and yield `{port: product}`. Everything opened is closed on exit."""
    wanted = tuple(p for p in PORTS if p in (ports or tuple(planned.bound)))
    missing = [p for p in wanted if p not in planned.bound]
    if missing:
        raise OverlayRefused(f"overlay {planned.environment} binds no adapter for {missing[0]!r}")
    given = hooks or {}
    built: dict[str, Any] = {}
    async with AsyncExitStack() as stack:
        for port in wanted:
            bound = planned.bound[port]
            mine = given.get(port, {})
            absent = [h for h in bound.adapter.hooks if h not in mine]
            if absent:
                raise OverlayRefused(f"{bound.adapter.name} ({port}) needs the hook {absent[0]!r}")
            settings = _resolved(bound, built)
            wiring = Wiring(settings, dict(built), mine, planned.environment, planned.base)
            built[port] = await stack.enter_async_context(bound.adapter.build(wiring))
        yield built


def _resolved(bound: Bound, built: Mapping[str, Any]) -> dict[str, Any]:
    reader = built.get("secrets")
    if reader is None and bound.adapter.port == "secrets":
        # The vault's own address from the deployment's environment (an azd
        # output), so no overlay hard-codes a globally unique vault name.
        from agent_harness.adapters.secrets import EnvironmentSecrets

        reader = EnvironmentSecrets()
    out: dict[str, Any] = {}
    for name, value in bound.settings.items():
        if is_reference(value):
            if reader is None:
                raise OverlayRefused(
                    f"{bound.adapter.name}.{name} is a reference and no secrets port is bound"
                )
            out[name] = reader.read(value)
        else:
            out[name] = value
    return out


__all__ = [
    "ADAPTERS",
    "PORTS",
    "Adapter",
    "Bound",
    "Build",
    "OverlayRefused",
    "Plan",
    "Setting",
    "Wiring",
    "compose",
    "is_reference",
    "plan",
]
