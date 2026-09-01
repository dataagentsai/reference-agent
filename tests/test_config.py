"""Configuration decides what a verdict means. These pin that, not the plumbing."""

from __future__ import annotations

import pytest

from support_agent.config import Settings, UnapprovedModel, resolve


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
@pytest.mark.discharges("AAC-0094")
def test_model_allowlist(name: str, model: str, allowed: bool) -> None:
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
    ("prompt version change", {"prompt_version": "v2"}, True),
    ("router rules change", {"router_rules_version": "v2"}, True),
    ("temperature change", {"temperature": 0.7}, True),
    ("step budget change", {"max_steps": 30}, True),
    ("resolution change", {"resolution": "mock"}, True),
    ("api key rotation", {"provider_api_key": "rotated"}, False),
    ("tool endpoint swap", {"mcp_base_url": "http://localhost:9999/mcp"}, False),
]


@pytest.mark.parametrize(
    ("name", "override", "should_change"),
    FINGERPRINT_CASES,
    ids=[c[0] for c in FINGERPRINT_CASES],
)
@pytest.mark.discharges("AAC-0012", "AAC-0107")
def test_fingerprint_tracks_behaviour_not_secrets(
    name: str, override: dict[str, object], should_change: bool
) -> None:
    baseline = resolve(settings()).fingerprint
    changed = resolve(settings(**override)).fingerprint
    assert (baseline != changed) is should_change


@pytest.mark.discharges("AAC-0012")
def test_fingerprint_is_stable_across_identical_resolutions() -> None:
    assert resolve(settings()).fingerprint == resolve(settings()).fingerprint


def test_swapping_the_tool_endpoint_is_the_only_change_agenttwin_makes() -> None:
    """N3: the agent is byte-identical between the real world and a simulated
    one, and the fingerprint says so — pointing at a projection server is not a
    different configuration of the agent."""
    real = resolve(settings(mcp_base_url="https://tools.internal/mcp"))
    simulated = resolve(settings(mcp_base_url="http://localhost:9040/mcp"))
    assert real.fingerprint == simulated.fingerprint
    assert real.mcp_base_url != simulated.mcp_base_url
