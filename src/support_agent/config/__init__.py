"""Which version is running.

L15 · P3. Model pins, prompt versions, budgets and flags, resolved once into a
frozen object with a stable fingerprint that is attached to every run.

The fingerprint is the point of this module. A verdict without the configuration
that produced it is not interpretable, and "it passed last week" is not a claim
you can check unless you can say what "it" was.

Sibling of `telemetry` and may not import it: configuration cannot log, and
logging cannot read configuration. The composition root hands each what it needs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from support_agent.contracts.failures import AgentFailure, Fault

ResolutionMode = Literal["mock", "replay", "real", "shadow"]


class UnapprovedModel(AgentFailure):
    """AAC-0094 — only approved models are reachable.

    An allowlist that is checked at startup rather than at call time, so a typo
    in an environment variable fails the process instead of failing a customer.
    """

    fault = Fault.MISCONFIGURED


class ProviderMismatch(AgentFailure):
    """The declared provider is not the one the endpoint routes to (T-018).

    Checked at startup like `UnapprovedModel`, for the same reason: a gateway
    whose routing moved under a fixed declaration is a different system wearing
    the old fingerprint, and a turn is the wrong place to find that out.
    """

    fault = Fault.MISCONFIGURED


class Budgets(BaseModel):
    """Ceilings that can actually stop a call.

    Two positions, deliberately. `max_output_tokens` is a per-call backstop at
    P3; `max_steps` and `max_cost_usd` are per-task and enforced at P4, because
    only the loop can see that fourteen calls are one runaway task rather than
    fourteen tasks. AAC-0093 and AAC-0008 are different obligations for exactly
    this reason.
    """

    model_config = ConfigDict(frozen=True)

    max_steps: int = 12
    max_cost_usd: float = 0.50
    max_output_tokens: int = 4096
    max_tool_result_chars: int = 8000
    """Tool results are bounded before they enter context — AAC-0105."""


class Settings(BaseSettings):
    """Environment-driven inputs. Nothing here is read anywhere but the
    composition root."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    model: str = "openai/gpt-oss-120b"
    approved_models: tuple[str, ...] = (
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    )
    """Verified against the provider's own /models endpoint on 2026-09-01.

    The previous list named `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`,
    which the provider no longer serves — every call returned 404. Nothing in a
    scripted test suite can catch a model being retired, which is precisely the
    class of failure the first live call exists to find. Re-check this list
    whenever a run starts failing at the provider rather than in the loop.
    """
    provider: str = "groq"
    """Who serves the model, **declared** rather than read off the URL (T-018).

    One URL used to carry two facts: which provider, and which route to it. A
    gateway in front of the same provider changes the route and not the system,
    and a URL cannot say which of the two happened. So the provider is stated
    here and checked at startup against what the endpoint reports serving
    (`RunConfig.check_served_by`), and the URL is left out of the fingerprint."""
    provider_base_url: str = "https://api.groq.com/openai/v1"
    """The route: the provider itself, or a gateway in front of it (T-029)."""
    provider_api_key: str = Field(default="", repr=False)

    mcp_base_url: str = "http://localhost:9040/mcp"
    """The one line AgentTwin swaps. Real tool server, or a projection of a
    declared world — the agent is byte-identical either way."""

    prompt_version: str = "v1"
    router_rules_version: str = "v1"
    """Routing changes are gated like model changes — AAC-0101."""

    resolution: ResolutionMode = "real"
    sealed: bool = False
    """Sealed mode fails closed: a system resolving to `real` inside a sealed run
    is an error, never a warning. The agent never knows which world it is in;
    the harness always does."""

    temperature: float = 0.0
    max_steps: int = 12
    max_cost_usd: float = 0.50

    otlp_endpoint: str = ""
    """Where spans go besides memory. Empty means nowhere, which is the honest
    default for a machine with no collector on it.

    Deliberately **not** part of the run fingerprint: pointing telemetry at a
    different backend does not change what the agent does, and a fingerprint
    that moved when it did would say two identical runs were different systems.
    Same argument as `mcp_base_url`, which `RunConfig.fingerprint` already
    excludes."""
    otlp_headers: str = Field(default="", repr=False)
    """`key=value,key=value` — an auth header for a hosted collector. `repr=False`
    for the same reason the provider key is."""


class RunConfig(BaseModel):
    """The resolved, frozen configuration for one run, plus its fingerprint."""

    model_config = ConfigDict(frozen=True)

    model: str
    provider: str
    provider_base_url: str
    mcp_base_url: str
    prompt_version: str
    router_rules_version: str
    resolution: ResolutionMode
    sealed: bool
    temperature: float
    budgets: Budgets

    @property
    def fingerprint(self) -> str:
        """A stable hash over everything that changes behaviour.

        Secrets and endpoints are excluded: a key rotation must not invalidate
        the claim that this is the same configuration, and a URL is a route, not
        a system. `mcp_base_url` is out because a world reached over another URL
        is the same world; `provider_base_url` is out because a gateway in front
        of the same provider is the same model. The provider itself is in,
        declared.

        **Changed 2026-09-16 (T-018).** Until then `provider_base_url` was
        hashed, so every fingerprint recorded before this date differs from one
        taken after it for the same configuration. No committed baseline carried
        one, which is why the break was taken now rather than later.
        """
        material = self.model_dump(mode="json", exclude={"mcp_base_url", "provider_base_url"})
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def check_served_by(self, served: str | None) -> bool:
        """Hold the declared provider to what the endpoint says it serves.

        Returns whether the declaration was verified. `None` means the endpoint
        could not say, which is the honest answer for a provider this agent does
        not recognise; the caller records it as unverified. A *different* answer
        fails the process: a declaration that disagrees with the route would put
        one fingerprint on two systems, which is the failure this field exists
        to prevent.
        """
        if served is None:
            return False
        if served != self.provider:
            raise ProviderMismatch(
                f"declared provider {self.provider!r}, but {self.provider_base_url} "
                f"serves {self.model!r} from {served!r}"
            )
        return True


def resolve(settings: Settings) -> RunConfig:
    """Validate and freeze. Fails at startup, never mid-conversation."""
    if settings.model not in settings.approved_models:
        raise UnapprovedModel(
            f"{settings.model!r} is not in the approved list {settings.approved_models}"
        )
    if settings.sealed and settings.resolution in ("real", "shadow"):
        raise ValueError(
            f"sealed run cannot use resolution={settings.resolution!r}: "
            "a sealed run makes no network egress and fails closed"
        )
    return RunConfig(
        model=settings.model,
        provider=settings.provider,
        provider_base_url=settings.provider_base_url,
        mcp_base_url=settings.mcp_base_url,
        prompt_version=settings.prompt_version,
        router_rules_version=settings.router_rules_version,
        resolution=settings.resolution,
        sealed=settings.sealed,
        temperature=settings.temperature,
        budgets=Budgets(
            max_steps=settings.max_steps,
            max_cost_usd=settings.max_cost_usd,
        ),
    )


__all__ = [
    "Budgets",
    "ProviderMismatch",
    "ResolutionMode",
    "RunConfig",
    "Settings",
    "UnapprovedModel",
    "resolve",
]
