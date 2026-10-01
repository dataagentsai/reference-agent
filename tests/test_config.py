"""Configuration decides what a verdict means. These pin that, not the plumbing."""

from __future__ import annotations

import pytest

from support_agent.config import ProviderMismatch, Settings, UnapprovedModel, resolve


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"provider_api_key": "test-key"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# AAC-0094 — only approved models are reachable, checked at startup so a typo
# fails the process instead of failing a customer.
# --------------------------------------------------------------------------- #

MODEL_CASES = [
    ("approved model resolves", "openai/gpt-oss-120b", True),
    ("second approved model resolves", "openai/gpt-oss-20b", True),
    ("unapproved model is rejected", "some-unvetted-model", False),
    ("near-miss typo is rejected", "openai/gpt-oss-120", False),
]


@pytest.mark.parametrize(("name", "model", "allowed"), MODEL_CASES, ids=[c[0] for c in MODEL_CASES])
@pytest.mark.discharges("AAC-0094", "Q-MODEL", "AHC-0004")
def test_model_allowlist(name: str, model: str, allowed: bool) -> None:
    """Also AHC-0004: the set of reachable models is enumerable from the choke
    point, and a model outside it fails at startup rather than at a provider."""
    if allowed:
        assert resolve(settings(model=model)).model == model
    else:
        with pytest.raises(UnapprovedModel):
            resolve(settings(model=model))


# --------------------------------------------------------------------------- #
# Sealed mode fails closed. A sealed run makes no network egress, so a
# resolution that would reach a real system is an error, never a warning.
# --------------------------------------------------------------------------- #

SEALED_CASES = [
    ("sealed + mock is fine", True, "mock", True),
    ("sealed + replay is fine", True, "replay", True),
    ("sealed + real fails closed", True, "real", False),
    ("sealed + shadow fails closed", True, "shadow", False),
    ("unsealed + real is fine", False, "real", True),
]


@pytest.mark.parametrize(
    ("name", "sealed", "resolution", "ok"), SEALED_CASES, ids=[c[0] for c in SEALED_CASES]
)
@pytest.mark.discharges("B3", "B5")
def test_sealed_mode_fails_closed(name: str, sealed: bool, resolution: str, ok: bool) -> None:
    if ok:
        assert resolve(settings(sealed=sealed, resolution=resolution)) is not None
    else:
        with pytest.raises(ValueError, match="fails closed"):
            resolve(settings(sealed=sealed, resolution=resolution))


# --------------------------------------------------------------------------- #
# The fingerprint. A verdict without the configuration that produced it is not
# interpretable — so anything that changes behaviour must change the hash, and
# anything that does not must not.
# --------------------------------------------------------------------------- #

FINGERPRINT_CASES = [
    ("model change", {"model": "openai/gpt-oss-20b"}, True),
    ("prompt version change", {"prompt_version": "v3"}, True),
    ("router rules change", {"router_rules_version": "v3"}, True),
    ("temperature change", {"temperature": 0.7}, True),
    ("step budget change", {"max_steps": 30}, True),
    ("resolution change", {"resolution": "mock"}, True),
    ("api key rotation", {"provider_api_key": "rotated"}, False),
    ("tool endpoint swap", {"mcp_base_url": "http://localhost:9999/mcp"}, False),
    # T-018: a URL is a route, the provider is the system.
    ("a different provider", {"provider": "together"}, True),
    (
        "a gateway in front of the same provider",
        {"provider_base_url": "http://localhost:4000/v1"},
        False,
    ),
]


@pytest.mark.parametrize(
    ("name", "override", "should_change"),
    FINGERPRINT_CASES,
    ids=[c[0] for c in FINGERPRINT_CASES],
)
@pytest.mark.discharges("AAC-0012", "AAC-0107", "AHC-0003")
def test_fingerprint_tracks_behaviour_not_secrets(
    name: str, override: dict[str, object], should_change: bool
) -> None:
    baseline = resolve(settings()).fingerprint
    changed = resolve(settings(**override)).fingerprint
    assert (baseline != changed) is should_change


@pytest.mark.discharges("AAC-0012", "AHC-0003")
def test_fingerprint_is_stable_across_identical_resolutions() -> None:
    assert resolve(settings()).fingerprint == resolve(settings()).fingerprint


@pytest.mark.discharges("B3", "AHC-0022")
def test_swapping_the_tool_endpoint_is_the_only_change_agenttwin_makes() -> None:
    """N3: the agent is byte-identical between the real world and a simulated
    one, and the fingerprint says so — pointing at a projection server is not a
    different configuration of the agent."""
    real = resolve(settings(mcp_base_url="https://tools.internal/mcp"))
    simulated = resolve(settings(mcp_base_url="http://localhost:9040/mcp"))
    assert real.fingerprint == simulated.fingerprint
    assert real.mcp_base_url != simulated.mcp_base_url


# --------------------------------------------------------------------------- #
# T-018 · The declared provider is held to what the endpoint serves. A mismatch
# fails at startup; an endpoint that cannot say is unverified, not a pass.
# --------------------------------------------------------------------------- #

SERVED_CASES = [
    ("the endpoint serves the declared provider", "groq", True),
    ("the endpoint cannot say", None, False),
]


@pytest.mark.parametrize(
    ("name", "served", "verified"), SERVED_CASES, ids=[c[0] for c in SERVED_CASES]
)
@pytest.mark.discharges("AAC-0012", "AHC-0003")
def test_a_declared_provider_is_checked_against_the_endpoint(
    name: str, served: str | None, verified: bool
) -> None:
    assert resolve(settings(provider="groq")).check_served_by(served) is verified


@pytest.mark.discharges("AAC-0012", "AHC-0003")
def test_a_gateway_routing_elsewhere_fails_at_startup() -> None:
    config = resolve(settings(provider="groq", provider_base_url="http://localhost:4000/v1"))
    with pytest.raises(ProviderMismatch, match="'together'"):
        config.check_served_by("together")


# (why, the routing label the configuration declares, the rules the agent loads,
# whether it starts) — F-070: the label is in the fingerprint and the rules are
# what run, and nothing held one to the other.
ROUTING = [
    ("label and rules agree", "v2", None, True),
    ("the label moved and the rules did not", "v3", None, False),
    ("the rules moved and the label did not", "v2", "v3", False),
]


@pytest.mark.parametrize(("why", "label", "loaded", "starts"), ROUTING, ids=[r[0] for r in ROUTING])
@pytest.mark.discharges("AHC-0100", "AHC-0032")
def test_the_routing_label_names_the_rules_that_run(
    why: str, label: str, loaded: str | None, starts: bool
) -> None:
    from support_agent import entrypoint as ep
    from support_agent import router
    from support_agent.config import RulesMismatch
    from support_agent.llm import ScriptedClient
    from support_agent.state import InMemoryCheckpointStore

    config = resolve(Settings(router_rules_version=label))
    rules = router.Rules(version=loaded) if loaded else None

    def build() -> object:
        return ep.build(
            llm=ScriptedClient([]),
            tools=None,  # type: ignore[arg-type]
            store=InMemoryCheckpointStore(),
            config=config,
            rules=rules,
        )

    if starts:
        build()
    else:
        with pytest.raises(RulesMismatch):
            build()
