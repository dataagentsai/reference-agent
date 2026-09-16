"""T-029: what the gateway's answers mean to the agent.

A gateway adds answers a provider alone never gave: a key refused a model, a
caller over its rate limit, a caller over its budget. Each has to reach the
agent as the right kind, because the kind decides whether `ResilientLLM` waits
and tries again. Two of them share HTTP 429, which is why the mapping is tested
by table rather than trusted.

The table runs offline. The rows after it run against the composed LiteLLM
proxy with short-lived keys, and skip when none is running.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator

import httpx2
import openai
import pytest

from support_agent.config import Settings, resolve
from support_agent.contracts import (
    Message,
    ModelBudgetExhausted,
    ModelMalformed,
    ModelRefused,
    ModelRequest,
    ModelThrottled,
    ModelUnavailable,
)
from support_agent.llm import GroqClient, connect_model, failure_from

PROXY = os.environ.get("AGENT_TEST_PROXY_URL", "http://localhost:4000")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "sk-local-dev-only")
AGENT_KEY = os.environ.get("AGENT_GATEWAY_KEY", "sk-support-agent-local-dev-only")
MODEL = "openai/gpt-oss-120b"
REQUEST = ModelRequest(messages=(Message(role="user", content="hi"),), max_tokens=5)


def status_error(
    status: int, body: dict[str, object] | None = None, **headers: str
) -> openai.APIError:
    request = httpx2.Request("POST", "http://gateway/v1/chat/completions")
    response = httpx2.Response(status, request=request, headers=headers)
    kind = {
        429: openai.RateLimitError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
    }
    return kind.get(status, openai.APIStatusError)(f"{status}", response=response, body=body)


# (why, the error, the kind it must become)
MAPPING = [
    (
        "a rate limit waits and retries",
        status_error(429, {"type": "throttling_error"}, **{"retry-after": "60"}),
        ModelThrottled,
    ),
    (
        "an exhausted budget is 429 too, and is not a rate limit",
        status_error(429, {"type": "budget_exceeded"}),
        ModelBudgetExhausted,
    ),
    ("a key the gateway does not accept", status_error(401, {"type": "auth_error"}), ModelRefused),
    (
        "a model this key may not use",
        status_error(403, {"type": "key_model_access_denied"}),
        ModelRefused,
    ),
    (
        "a request the provider will not take",
        status_error(400, {"type": "invalid_request_error"}),
        ModelRefused,
    ),
    ("a timeout is not a refusal", status_error(408), ModelUnavailable),
    ("the provider is down", status_error(503), ModelUnavailable),
    (
        "nothing answered",
        openai.APIConnectionError(request=httpx2.Request("POST", "http://gateway")),
        ModelUnavailable,
    ),
]


@pytest.mark.parametrize(("why", "error", "kind"), MAPPING, ids=[m[0] for m in MAPPING])
@pytest.mark.discharges("AHC-0021", "AHC-0110")
def test_every_gateway_answer_becomes_its_own_kind(
    why: str, error: openai.APIError, kind: type
) -> None:
    failure = failure_from(error)
    assert type(failure) is kind


def test_a_rate_limit_keeps_the_gateways_retry_after() -> None:
    failure = failure_from(status_error(429, {"type": "throttling_error"}, **{"retry-after": "60"}))
    assert isinstance(failure, ModelThrottled)
    assert failure.retry_after == 60.0


# --------------------------------------------------------------------------- #
# Against the composed proxy.
# --------------------------------------------------------------------------- #


def admin(path: str, body: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"{PROXY}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"no LiteLLM proxy at {PROXY}: {exc}")


@pytest.fixture
def key() -> Iterator[object]:
    """Make a short-lived key with the limits a row asks for; delete it after."""
    made: list[str] = []

    def make(**limits: object) -> str:
        value = f"sk-test-{uuid.uuid4().hex}"
        admin("/key/generate", {"key": value, "models": [MODEL], **limits})
        made.append(value)
        return value

    yield make
    if made:
        admin("/key/delete", {"keys": made})


def client(api_key: str, model: str = MODEL) -> GroqClient:
    return GroqClient(api_key=api_key, base_url=f"{PROXY}/v1", model=model)


# (why, the key's limits, the model asked for, the kind the agent must see)
LIVE = [
    ("a caller over its budget", {"max_budget": 0.0}, MODEL, ModelBudgetExhausted),
    ("a caller over its rate limit", {"rpm_limit": 0}, MODEL, ModelThrottled),
    ("a model outside the key's allowlist", {}, "openai/gpt-oss-20b", ModelRefused),
]


@pytest.mark.parametrize(("why", "limits", "model", "kind"), LIVE, ids=[r[0] for r in LIVE])
@pytest.mark.discharges("AHC-0021", "AAC-0104")
async def test_the_composed_gateway_answers_reach_the_agent_as_their_kind(
    key, why: str, limits: dict[str, object], model: str, kind: type
) -> None:
    with pytest.raises(ModelUnavailable) as raised:
        await client(key(**limits), model).complete(REQUEST)
    assert type(raised.value) is kind


@pytest.mark.discharges("AHC-0005")
async def test_a_refused_call_does_not_put_the_gateway_on_cooldown(key) -> None:
    """Measured 16 Sep before `disable_cooldowns`: a provider refusal put the
    deployment on cooldown, and the next call came back as a 429, a rate limit,
    for a failure no amount of waiting would fix. Only meaningful with no
    provider key in the proxy, when the provider refuses every call."""
    caller = client(key())
    outcomes = []
    for _ in range(2):
        try:
            await caller.complete(REQUEST)
            outcomes.append(None)
        except ModelUnavailable as exc:
            outcomes.append(type(exc))
        except ModelMalformed:
            # The provider answered: five tokens is less than a reasoning model
            # spends before it replies. Served, not refused.
            outcomes.append(None)
    if None in outcomes:
        # Since 17 Sep the local proxy holds a working key, so this skips there.
        # The property was checked when it did not: with cooldowns switched back
        # on, the second call came back as ModelThrottled.
        pytest.skip("the proxy holds a provider key, so no call is refused")
    assert outcomes == [ModelRefused, ModelRefused]


@pytest.mark.discharges("AAC-0012", "AHC-0003")
async def test_the_agents_own_gateway_key_is_verified_at_startup() -> None:
    """The key compose's litellm-keys creates, used the way .env.example says."""
    config = resolve(Settings(provider_base_url=f"{PROXY}/v1"))
    try:
        _, verified = await connect_model(config, api_key=AGENT_KEY)
    except ModelUnavailable as exc:
        pytest.skip(f"no LiteLLM proxy at {PROXY}: {exc}")
    assert verified
