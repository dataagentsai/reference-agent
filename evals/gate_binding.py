"""This agent, as a runner that has never seen it drives it — `agenttwin.Binding`.

`evals.simulation.subject_for` is this agent's wiring against a world, and the
regression suite hands it a scripted client in process. This is the same wiring
with one difference that matters: the model is the **provider twin**, reached
over HTTP through this agent's *production* provider adapter (`GroqClient`, an
OpenAI-compatible client) wrapped in `ResilientLLM` exactly as the deployment
wraps it. So a throttle is a real 429 and the adapter's own error mapping is on
the path — which the in-process scripted client skips.

    uv run python -m agenttwin run --binding evals.gate_binding:open_subject scenarios/

It is the reference answer to the question every generated agent must also
answer, and the gates (`clean-ai-engineering/tools/gates`) name it in
`gates.yaml`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from agenttwin import Clock, Live, ModelEndpoint, Subject

from agent_harness.llm import GroqClient
from evals.simulation import subject_for


@asynccontextmanager
async def open_subject(
    live: Live, *, wrap: Callable[..., Any], clock: Clock, model: ModelEndpoint
) -> AsyncIterator[Subject]:
    llm = GroqClient(
        api_key=model.api_key,
        base_url=model.base_url,
        model=model.model,
        provider="agenttwin",
    )
    async with subject_for(live, llm=llm, clock=clock, wrap=wrap) as subject:
        yield subject


__all__ = ["open_subject"]
