"""F-022 — the provider behind retries, a throttle and a breaker, actually wired.

Each row scripts what the provider does, call by call, and states what the
decorator must do about it. The three conditions are kept apart on purpose: a
rate limit is not a failure, a failure is not malformed output, and an open
breaker is not a call at all.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from support_agent.contracts import (
    Message,
    ModelBudgetExhausted,
    ModelMalformed,
    ModelRefused,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
)
from support_agent.resilience import Backoff, CircuitBreaker, ResilientLLM

OK = ModelResponse(text="fine")
REQUEST = ModelRequest(messages=(Message(role="user", content="hi"),))


class Provider:
    """Plays a script of outcomes: a response to return or an exception to raise."""

    def __init__(self, script: list[ModelResponse | Exception]) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def down() -> ModelUnavailable:
    return ModelUnavailable("503")


# (name, script, raises, provider calls, breaker still closed, seconds slept at least)
CASES: list[tuple[str, list, type[Exception] | None, int, bool, float]] = [
    ("one transient failure is retried and recovers", [down(), OK], None, 2, True, 0.0),
    (
        "persistent failure gives up typed after 3 attempts",
        [down(), down(), down()],
        ModelUnavailable,
        3,
        False,
        0.0,
    ),
    (
        "a rate limit waits the provider's retry-after and is not a breaker failure",
        [ModelThrottled("429", retry_after=2.5), OK],
        None,
        2,
        True,
        2.5,
    ),
    ("malformed output is not retried", [ModelMalformed("junk")], ModelMalformed, 1, True, 0.0),
    # T-029: reached, and the answer was no. Neither is retried, and neither is
    # evidence the provider is down, so the breaker stays closed.
    ("a refusal is not retried", [ModelRefused("401 bad key")], ModelRefused, 1, True, 0.0),
    (
        "an exhausted budget is not retried",
        [ModelBudgetExhausted("429 budget_exceeded")],
        ModelBudgetExhausted,
        1,
        True,
        0.0,
    ),
]


@pytest.mark.discharges("AHC-0005", "AHC-0021", "AHC-0024", "AAC-0009")
@pytest.mark.parametrize(
    ("name", "script", "raises", "calls", "closed", "slept"), CASES, ids=[c[0] for c in CASES]
)
async def test_each_provider_condition_gets_its_own_answer(
    name: str, script: list, raises: type[Exception] | None, calls: int, closed: bool, slept: float
) -> None:
    provider = Provider(script)
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    llm = ResilientLLM(
        provider,
        backoff=Backoff(base_s=0.01, jitter=0.0),
        breaker=CircuitBreaker(threshold=3, cooldown_s=60),
        sleep=sleep,
    )
    if raises is None:
        assert await llm.complete(REQUEST) == OK
    else:
        with pytest.raises(raises):
            await llm.complete(REQUEST)

    assert provider.calls == calls
    assert llm.breaker.closed is closed
    assert sum(pauses) >= slept


@pytest.mark.discharges("AHC-0005")
async def test_an_open_breaker_fails_fast_without_calling_the_provider() -> None:
    provider = Provider([down()])
    now: Callable[[], float] = lambda: 0.0  # noqa: E731 — a frozen clock keeps it open
    llm = ResilientLLM(provider, attempts=1, breaker=CircuitBreaker(threshold=1, now=now))

    with pytest.raises(ModelUnavailable):
        await llm.complete(REQUEST)  # the failure that opens it
    with pytest.raises(ModelUnavailable, match="not answering"):
        await llm.complete(REQUEST)  # refused at the breaker
    assert provider.calls == 1, "an open breaker must not call the provider"


@pytest.mark.discharges("AHC-0005", "AHC-0024", "AAC-0009")
async def test_a_provider_blip_does_not_reach_the_customer() -> None:
    """Through the entrypoint: one 503, then an answer — the customer sees the answer."""
    from support_agent import entrypoint as ep
    from support_agent import identity as ident
    from support_agent.contracts import Completed, Identity
    from support_agent.state import InMemoryCheckpointStore

    provider = Provider([down(), ModelResponse(text="Happy to help with that.")])

    async def no_wait(_: float) -> None:
        return None

    class NoTools:
        async def list_tools(self, identity):  # noqa: ANN001, ANN202
            from support_agent.contracts import ToolRegistry

            return ToolRegistry()

    agent = ep.build(
        llm=ResilientLLM(provider, sleep=no_wait),
        tools=NoTools(),
        store=InMemoryCheckpointStore(),
    )
    result, _ = await agent.handle(
        "I need some help with a couple of my recent purchases",
        identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
    )
    assert isinstance(result, Completed) and result.reply == "Happy to help with that."
    assert provider.calls == 2


@pytest.mark.discharges("AHC-0024")
async def test_two_conversations_on_one_client_number_their_own_retries() -> None:
    """F-067. The try count lived on the client, and `complete` reset it: one
    `ResilientLLM` serves every conversation in the process, so a second call
    starting while the first was retrying renumbered the first one's retries —
    and could swallow the span of a retry that did happen."""
    import asyncio

    from support_agent import telemetry as tel

    exporter = tel.configure()

    class Blip:
        """Fails each request's first two attempts, letting others run between."""

        def __init__(self) -> None:
            self.failed: dict[str, int] = {}

        async def complete(self, request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(0)
            said = request.messages[0].content
            if self.failed.get(said, 0) < 2:
                self.failed[said] = self.failed.get(said, 0) + 1
                await asyncio.sleep(0)
                raise down()
            return OK

    async def no_wait(_: float) -> None:
        await asyncio.sleep(0)

    llm = ResilientLLM(Blip(), backoff=Backoff(base_s=0.0, jitter=0.0), sleep=no_wait)
    asks = [ModelRequest(messages=(Message(role="user", content=who),)) for who in "ab"]
    await asyncio.gather(*(llm.complete(ask) for ask in asks))

    numbered = [
        tel.attributes_of(s)["agent.retry.attempt"]
        for s in exporter.get_finished_spans()
        if s.name == "agent.llm.retry"
    ]
    assert sorted(numbered) == [1, 1, 2, 2], f"each conversation numbers its own: {numbered}"
