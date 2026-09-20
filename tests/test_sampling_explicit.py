"""Sampling parameters are explicit on every call, never inherited (AHC-0014).

The capability had a test once. It was renamed or removed at some point, the
generated assurance map was not regenerated for a while, and the loss was
invisible until the map was rebuilt — which is the failure the map exists to
catch, arriving late. This is the test, written back.

What it holds: whatever a caller asks for, a temperature crosses the wire on
every call. A provider's own default is not a decision this system has made,
and a run whose sampling came from somewhere nobody declared is a run nobody
can reproduce.
"""

from __future__ import annotations

from typing import Any

import pytest

from support_agent.contracts import ModelRequest
from support_agent.llm import GroqClient


class Recorded:
    """The provider SDK, replaced by something that writes down what it was
    sent and answers the shape `_from_wire` expects."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    class _Message:
        content = "fine"
        tool_calls = None

    class _Choice:
        message = None
        finish_reason = "stop"

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        choice = Recorded._Choice()
        choice.message = Recorded._Message()  # type: ignore[assignment]
        return type(
            "Answer",
            (),
            {
                "choices": [choice],
                "model": "openai/gpt-oss-120b",
                "usage": type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})(),
            },
        )()


def client_with(recorder: Recorded, *, temperature: float) -> GroqClient:
    made = GroqClient(
        api_key="unused",
        base_url="http://provider.test/v1",
        model="openai/gpt-oss-120b",
        temperature=temperature,
    )
    made._client.chat.completions.create = recorder.create  # type: ignore[method-assign]
    return made


# (why, the client's declared temperature, what the request asks for, what is sent)
ROWS = [
    ("the configured temperature is sent", 0.0, 0.0, 0.0),
    ("a request may raise it for one call", 0.0, 0.7, 0.7),
    ("a configured non-zero default is sent", 0.3, 0.0, 0.3),
]


@pytest.mark.parametrize(("why", "configured", "asked", "sent"), ROWS, ids=[r[0] for r in ROWS])
@pytest.mark.discharges("AHC-0014")
async def test_every_model_call_states_its_sampling(
    why: str, configured: float, asked: float, sent: float
) -> None:
    recorder = Recorded()
    await client_with(recorder, temperature=configured).complete(
        ModelRequest(messages=(), tools=(), temperature=asked)
    )
    assert "temperature" in recorder.kwargs, "the call inherited the provider's default"
    assert recorder.kwargs["temperature"] == sent
