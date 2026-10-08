"""Who serves the model behind a route, as the route itself can say (T-018).

Split from `llm` when the Azure stack's client arrived (T-099), so both
provider clients read the same answers: a provider's own endpoint is recognised
by its host, and a LiteLLM-style gateway says where it routes through
`/model/info`. Anything else is `None` — unverified, never a guess.
"""

from __future__ import annotations

from urllib.parse import urlparse

PROVIDER_HOSTS = {
    "api.groq.com": "groq",
    "api.together.xyz": "together",
    "api.cerebras.ai": "cerebras",
}
"""Providers recognisable by their own endpoint. A gateway is not listed: it says
where it routes through `/model/info`, and its host says nothing about that."""


def provider_from_host(base_url: str) -> str | None:
    """The provider a direct endpoint belongs to, or `None` if it is not one we
    recognise. Never a guess from a substring: `groq.example.com` is not Groq."""
    return PROVIDER_HOSTS.get(urlparse(base_url).hostname or "")


def provider_from_model_info(payload: object, model: str) -> str | None:
    """The provider a LiteLLM-style gateway routes `model` to, read from its
    `/model/info`. `None` when the payload does not name the model, which is
    unverified, not a match."""
    data = payload.get("data") if isinstance(payload, dict) else None
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or entry.get("model_name") != model:
            continue
        info = entry.get("model_info")
        provider = info.get("litellm_provider") if isinstance(info, dict) else None
        if isinstance(provider, str) and provider:
            return provider
    return None


__all__ = ["PROVIDER_HOSTS", "provider_from_host", "provider_from_model_info"]
