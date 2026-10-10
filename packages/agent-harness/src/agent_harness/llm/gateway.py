"""What a gateway in front of the provider said about one call (claims-fnol-azure A9).

APIM screens every prompt and completion with Content Safety and says what it
found in a response header, `x-content-safety` (`pass`, `block:<category>` or
`unavailable`, then the prompt's and the completion's own parts). The provider's
answer is the body; this is the gateway's, and it was dropped at the client, so
no check could ever record it.

`SIGNALS` names the headers carried, by prefix: a declared list, so what is
copied off the wire is data somebody chose and not every header a proxy adds.
Each client puts them on `ModelResponse.gateway` and on its `gen_ai.chat` span
(`agent.gateway.<header>`), where the watch reads them back for the `online`
position's `guardrail_log` evaluator. On a refused call there is no response, so
the span is the only record: a prompt Content Safety blocked is still seen.

**Two ways off the wire, one shape.** `GroqClient` reads the raw response's
headers itself. Pydantic AI's direct API returns no headers, so its client is
handed an HTTP client that `listening` has added a response hook to; the hook
writes into the dict `heard` opened for that call. A context variable keeps two
concurrent calls apart: each awaits its own request in its own context.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

SIGNALS = ("x-content-safety",)
"""The header-name prefixes a gateway's verdicts arrive under."""

SPAN_PREFIX = "agent.gateway."
"""A signal's span attribute is this and its header name: `agent.gateway.x-content-safety`."""

_LONGEST = 256
_heard: ContextVar[dict[str, str] | None] = ContextVar("gateway_heard", default=None)


def signals(headers: Mapping[str, str] | Any) -> dict[str, str]:
    """The declared signals among `headers`, by lower-cased name. Empty for none."""
    if headers is None:
        return {}
    found = {}
    for name, value in headers.items():
        key = str(name).lower()
        if key.startswith(SIGNALS):
            found[key] = str(value)[:_LONGEST]
    return found


def record(span: Any, heard: Mapping[str, str]) -> None:
    """Each signal onto the call's span."""
    for name, value in heard.items():
        span.set_attribute(SPAN_PREFIX + name, value)


def recorded(attributes: Mapping[str, Any]) -> dict[str, str]:
    """The signals a span carries, back by header name: what the watch reads."""
    return {
        k[len(SPAN_PREFIX) :]: str(v) for k, v in attributes.items() if k.startswith(SPAN_PREFIX)
    }


@contextmanager
def heard() -> Iterator[dict[str, str]]:
    """The signals of the one call made inside, filled by `listening`'s hook."""
    found: dict[str, str] = {}
    token = _heard.set(found)
    try:
        yield found
    finally:
        _heard.reset(token)


async def _on_response(response: Any) -> None:
    found = _heard.get()
    if found is not None:
        found.update(signals(getattr(response, "headers", None)))


def listening[C](http_client: C) -> C:
    """`http_client` (an async HTTP client with event hooks), with the hook that
    hands each response's signals to the call that `heard` opened."""
    hooks = dict(getattr(http_client, "event_hooks"))  # noqa: B009 — any async client with hooks
    if _on_response not in hooks.get("response", []):
        hooks["response"] = [*hooks.get("response", []), _on_response]
        setattr(http_client, "event_hooks", hooks)  # noqa: B010 — the same duck type
    return http_client


__all__ = ["SIGNALS", "SPAN_PREFIX", "heard", "listening", "record", "recorded", "signals"]
