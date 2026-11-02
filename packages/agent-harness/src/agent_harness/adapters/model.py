"""The `model` port: an `LLMClient`, behind the harness's retries and breaker.

    scripted          a script of answers: no network, no cost (tests, gates)
    litellm-proxy     the Open Stack's gateway, through the OpenAI SDK client
    groq-direct       Groq's OpenAI-compatible endpoint through Pydantic AI,
                      with no gateway (a developer's Mac)
    apim-ai-gateway   APIM in front of the backend, through Pydantic AI, the key
                      in APIM's subscription header (the Azure stack)
    anthropic         Claude through the Anthropic SDK's Messages API; `base_url`
                      reaches Anthropic directly or any Messages-API endpoint
                      (Databricks Model Serving). Run with an append-only
                      transcript (`Budgets.max_context_chars=None`).
    agentcore-gateway AgentCore Gateway's OpenAI-compatible inference endpoint,
                      through Pydantic AI, each request signed with the ECS
                      task's role (SigV4), or a bearer token (the AWS stack)

Which model, which provider and what temperature are the agent's resolved
configuration (`llm.ModelChoice`, handed in as the hook `choice`), so its
approved-model check and fingerprint stay where they are; the overlay says only
where the model is reached and with which key. The optional hook `http_client`
is a transport for the SDK (a contract test's, answering with no network).
Every network adapter is wrapped in `ResilientLLM`; a script is not, because
exhausting it is a test's mistake, not an outage to retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2

from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring
from agent_harness.contracts import LLMClient
from agent_harness.resilience import ResilientLLM

APIM_KEY_HEADER = "Ocp-Apim-Subscription-Key"
GROQ = "https://api.groq.com/openai/v1"


@asynccontextmanager
async def _scripted(wiring: Wiring) -> AsyncIterator[LLMClient]:
    from agent_harness.llm import ScriptedClient

    yield ScriptedClient(wiring.hooks["script"])


@asynccontextmanager
async def _litellm(wiring: Wiring) -> AsyncIterator[LLMClient]:
    from agent_harness.llm import GroqClient

    choice: Any = wiring.hooks["choice"]
    client = GroqClient(
        api_key=str(wiring.settings["api_key"]),
        base_url=str(wiring.settings["base_url"]),
        model=choice.model,
        provider=choice.provider,
        temperature=choice.temperature,
        http_client=wiring.hooks.get("http_client"),
    )
    yield ResilientLLM(client)


def _pydantic_ai(wiring: Wiring, *, key_header: str | None) -> LLMClient:
    from agent_harness.llm.pydantic_ai import openai_compatible

    choice: Any = wiring.hooks["choice"]
    return ResilientLLM(
        openai_compatible(
            api_key=str(wiring.settings["api_key"]),
            base_url=str(wiring.settings["base_url"]),
            model=choice.model,
            provider=choice.provider,
            temperature=choice.temperature,
            key_header=key_header,
            http_client=wiring.hooks.get("http_client"),
        )
    )


@asynccontextmanager
async def _groq(wiring: Wiring) -> AsyncIterator[LLMClient]:
    yield _pydantic_ai(wiring, key_header=None)


@asynccontextmanager
async def _apim(wiring: Wiring) -> AsyncIterator[LLMClient]:
    yield _pydantic_ai(wiring, key_header=str(wiring.settings["key_header"]))


@asynccontextmanager
async def _anthropic(wiring: Wiring) -> AsyncIterator[LLMClient]:
    from agent_harness.llm.anthropic import AnthropicClient

    choice: Any = wiring.hooks["choice"]
    fallbacks = wiring.settings.get("fallbacks")
    client = AnthropicClient(
        model=choice.model,
        api_key=str(wiring.settings["api_key"]),
        base_url=wiring.settings.get("base_url") or None,
        effort=wiring.settings.get("effort") or None,
        fallbacks=str(fallbacks) if fallbacks else None,
        client=wiring.hooks.get("client"),
    )
    yield ResilientLLM(client)


@asynccontextmanager
async def _agentcore(wiring: Wiring) -> AsyncIterator[LLMClient]:
    """AgentCore Gateway's OpenAI-compatible `/inference/v1`. With `auth: iam`
    (the default) every request is signed with the task role (SigV4): no key
    exists to leak. With `auth: jwt`, `api_key` is a bearer token the gateway's
    authorizer accepts. Either way the provider's own key (Groq's) stays in the
    gateway's credential store, never here."""
    from agent_harness.adapters import aws
    from agent_harness.llm.pydantic_ai import openai_compatible

    settings = wiring.settings
    base_url = str(settings["base_url"])
    mode = str(settings["auth"])
    http: Any = wiring.hooks.get("http_client")
    if mode == "iam":
        region = settings.get("region") or aws.region_of_gateway(base_url)
        if not region:
            raise OverlayRefused("agentcore-gateway: give `region`, or a gateway URL that names it")
        credentials = wiring.hooks.get("credentials") or aws.role_credentials()
        http = http or httpx2.AsyncClient()
        http.auth = aws.SigV4(credentials, str(region), aws.AGENTCORE_SERVICE)
        key = "signed-by-sigv4"  # the SDK wants a key; the signature replaces it
    elif mode == "jwt":
        if not settings.get("api_key"):
            raise OverlayRefused("agentcore-gateway: auth jwt needs `api_key` (a reference)")
        key = str(settings["api_key"])
    else:
        raise OverlayRefused(f"agentcore-gateway: auth is iam or jwt, not {mode!r}")
    choice: Any = wiring.hooks["choice"]
    yield ResilientLLM(
        openai_compatible(
            api_key=key,
            base_url=base_url,
            model=choice.model,
            provider=choice.provider,
            temperature=choice.temperature,
            http_client=http,
        )
    )


KEYED = {"api_key": Setting(required=True, secret=True)}

SCRIPTED = Adapter("model", "scripted", _scripted, hooks=("script",))
LITELLM_PROXY = Adapter(
    "model",
    "litellm-proxy",
    _litellm,
    {**KEYED, "base_url": Setting(required=True)},
    hooks=("choice",),
)
GROQ_DIRECT = Adapter(
    "model", "groq-direct", _groq, {**KEYED, "base_url": Setting(default=GROQ)}, hooks=("choice",)
)
APIM = Adapter(
    "model",
    "apim-ai-gateway",
    _apim,
    {**KEYED, "base_url": Setting(required=True), "key_header": Setting(default=APIM_KEY_HEADER)},
    hooks=("choice",),
)

ANTHROPIC = Adapter(
    "model",
    "anthropic",
    _anthropic,
    {
        **KEYED,
        "base_url": Setting(default=""),
        "effort": Setting(default="medium"),
        "fallbacks": Setting(default=""),
    },
    hooks=("choice",),
)
AGENTCORE_GATEWAY = Adapter(
    "model",
    "agentcore-gateway",
    _agentcore,
    {
        "base_url": Setting(required=True),
        "auth": Setting(default="iam"),
        "region": Setting(),
        "api_key": Setting(secret=True),
    },
    hooks=("choice",),
)

__all__ = [
    "AGENTCORE_GATEWAY",
    "ANTHROPIC",
    "APIM",
    "APIM_KEY_HEADER",
    "GROQ_DIRECT",
    "LITELLM_PROXY",
    "SCRIPTED",
]
