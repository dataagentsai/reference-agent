"""T-018: who actually serves the model, read from the endpoint rather than assumed.

The pure readers are checked by table. The client's own request is checked
against the composed LiteLLM proxy when one is running, and skips otherwise,
the way the database tests do.
"""

from __future__ import annotations

import os

import pytest

from support_agent.config import ProviderMismatch, Settings, resolve
from support_agent.llm import connect_model, provider_from_host, provider_from_model_info

PROXY = os.environ.get("AGENT_TEST_PROXY_URL", "http://localhost:4000/v1")
PROXY_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-local-dev-only")

HOST_CASES = [
    ("Groq's own endpoint", "https://api.groq.com/openai/v1", "groq"),
    ("Together's own endpoint", "https://api.together.xyz/v1", "together"),
    ("a gateway says nothing by its host", "http://localhost:4000/v1", None),
    ("a lookalike host is not the provider", "https://groq.example.com/v1", None),
]


@pytest.mark.parametrize(("name", "url", "provider"), HOST_CASES, ids=[c[0] for c in HOST_CASES])
@pytest.mark.discharges("AHC-0003")
def test_a_direct_endpoint_is_recognised_by_its_host(
    name: str, url: str, provider: str | None
) -> None:
    assert provider_from_host(url) == provider


def entry(model: str, provider: object) -> dict[str, object]:
    return {"model_name": model, "model_info": {"litellm_provider": provider}}


MODEL_INFO_CASES = [
    ("the gateway routes the model to groq", {"data": [entry("m", "groq")]}, "groq"),
    (
        "the model is found among others",
        {"data": [entry("x", "openai"), entry("m", "groq")]},
        "groq",
    ),
    ("the model is not listed", {"data": [entry("x", "groq")]}, None),
    ("a listed model with no provider", {"data": [{"model_name": "m"}]}, None),
    ("an empty provider", {"data": [entry("m", "")]}, None),
    ("not the shape expected", ["m", "groq"], None),
    ("no data at all", {}, None),
]


@pytest.mark.parametrize(
    ("name", "payload", "provider"), MODEL_INFO_CASES, ids=[c[0] for c in MODEL_INFO_CASES]
)
@pytest.mark.discharges("AHC-0003")
def test_a_gateway_says_where_it_routes(name: str, payload: object, provider: str | None) -> None:
    assert provider_from_model_info(payload, "m") == provider


async def _proxy_or_skip(declared: str) -> tuple[object, bool]:
    config = resolve(Settings(provider=declared, provider_base_url=PROXY))
    try:
        return await connect_model(config, api_key=PROXY_KEY)
    except ProviderMismatch:
        raise
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no LiteLLM proxy at {PROXY}: {exc}")


PROXY_CASES = [
    ("the composed proxy routes to groq, as declared", "groq", True),
    ("a declaration the proxy contradicts fails at startup", "together", False),
]


@pytest.mark.parametrize(("name", "declared", "ok"), PROXY_CASES, ids=[c[0] for c in PROXY_CASES])
@pytest.mark.discharges("AAC-0012", "AHC-0003")
async def test_the_composed_proxy_is_checked_at_startup(name: str, declared: str, ok: bool) -> None:
    if ok:
        _, verified = await _proxy_or_skip(declared)
        assert verified
    else:
        with pytest.raises(ProviderMismatch):
            await _proxy_or_skip(declared)
