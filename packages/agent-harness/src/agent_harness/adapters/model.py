"""The `model` port: an `LLMClient`, behind the harness's retries and breaker.

    scripted          a script of answers: no network, no cost (tests, gates)
    litellm-proxy     the Open Stack's gateway, through the OpenAI SDK client
    groq-direct       Groq's OpenAI-compatible endpoint through Pydantic AI,
                      with no gateway (a developer's Mac)
    apim-ai-gateway   APIM in front of the backend, through Pydantic AI, the key
                      in APIM's subscription header (the Azure stack)

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

from agent_harness.adapters import Adapter, Setting, Wiring
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

__all__ = ["APIM", "APIM_KEY_HEADER", "GROQ_DIRECT", "LITELLM_PROXY", "SCRIPTED"]
