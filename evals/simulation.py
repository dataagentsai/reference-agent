"""This implementation, as a scenario sees it — the far side of the agent contract.

**Outside `src/` deliberately.** The import contract says *the agent cannot see
its simulator*, and this module is where the two meet — so it lives beside the
evaluation code that already imports both. Putting it in the package would have
let the agent reach its own twin, which is the one direction that must never
work: a system that can see its simulator can be written to pass it.

A declared scenario names a world, a customer and what it expects, and knows
nothing about how this agent is built. Everything it does not know lives here:
which provider, which stores, which scopes the projected world is gated with,
how an identity is minted from a customer id.

**That is the whole point of the split.** A regenerated agent will make different
choices at every line below, and the same scenario file has to drive it — so the
contract is three callables and this module is the only thing that changes.

    async with subject_for(live, llm=..., clock=...) as subject:
        record, outcomes = await run_file(path, subject=subject)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from agenttwin import Approver, Desk, Live, Subject, project

from evals import durable
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent.binding import SCOPES
from support_agent.config import RunConfig
from support_agent.contracts import (
    Clock,
    Identity,
    LLMClient,
    Message,
    ModelMalformed,
    ModelRequest,
    ModelResponse,
    ModelThrottled,
    ModelUnavailable,
)
from support_agent.cost import Meter
from support_agent.idempotency import InMemoryLedger
from support_agent.resilience import ResilientLLM
from support_agent.state import Conversation, InMemoryCheckpointStore
from support_agent.tools import connect

DECISIONS = {
    "grant": Approver.grants,
    "refuse": Approver.denies,
    "never": Approver.silent,
    "grant-twice": Approver.grants_twice,
}
RESOLUTIONS = {"handled": Desk.answers, "never": Desk.never_comes}


@dataclass
class FaultyProvider:
    """A model client that misbehaves on the calls a scenario named.

    **The provider is an external system the agent depends on**, and a world that
    could perturb every system except that one left the dependency most likely to
    fail outside the simulation. The scenario declares a *kind*; this maps it onto
    the exception this agent's adapter raises — the same division as scope names,
    where the world says what goes wrong and the binding says what it is called
    here.
    """

    inner: LLMClient
    faults: dict[int, tuple[str, float | None, int]]
    clock: Clock | None = None
    calls: int = 0
    fired: list[int] = field(default_factory=list)
    outage: tuple[str, float | None, int] | None = None
    """A fault that lasts (`lasts_s`): what it is, and the clock time it ends."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        now = self.clock() if self.clock is not None else 0
        fault = self.faults.get(self.calls)
        if fault is not None:
            self.fired.append(self.calls)
            kind, retry_after, lasts_s = fault
            if lasts_s:
                self.outage = (kind, retry_after, now + lasts_s)
        elif self.outage is not None and now < self.outage[2]:
            kind, retry_after, _ = self.outage
        else:
            return await self.inner.complete(request)
        if kind == "provider_throttled":
            raise ModelThrottled("the provider is rate limiting", retry_after=retry_after)
        if kind == "provider_unavailable":
            raise ModelUnavailable("the provider could not be reached")
        raise ModelMalformed("the provider returned something unreadable", raw="{not json")

    @property
    def unfired(self) -> tuple[int, ...]:
        return tuple(call for call in self.faults if call not in self.fired)


@asynccontextmanager
async def subject_for(
    live: Live,
    *,
    llm: LLMClient,
    clock: Clock | None = None,
    wrap: object = None,
    provider_faults: tuple[tuple[int, str, float | None, int], ...] = (),
    config: RunConfig | None = None,
    meters: list[Meter] | None = None,
) -> AsyncIterator[Subject]:
    """Wire this agent against a live world and hand back what a scenario drives.

    `wrap` is the scenario's faults, and it is **opaque here on purpose**: a
    perturbation happens to the system the agent depends on, so the world owns
    it and this side neither interprets it nor knows it is there. Forwarded
    to the projection and never inspected."""
    # A meter is made per unit of work, so the only way to know what a scenario
    # cost is to keep the ones this run made. Kept here rather than on the
    # contract: what a run costs is real, and it is not something a scenario
    # should be able to see, or it becomes something a scenario can assert on
    # and then nobody can change the prompt.
    metering = None
    if config is not None:

        def metering() -> Meter:  # noqa: F811 — the None case is the default
            made = Meter(config.model, ceiling_usd=config.budgets.max_cost_usd)
            if meters is not None:
                meters.append(made)
            return made

    if provider_faults:
        faults = {call: (kind, after, lasts) for call, kind, after, lasts in provider_faults}
        llm = FaultyProvider(llm, faults, clock=clock)
    # Wrapped exactly as the deployment wraps it (F-029). A simulation that
    # composes the agent differently from production is simulating a different
    # agent, and the difference is invisible until a scenario asks the provider
    # to misbehave — at which point the *harness* degrades and reads as the
    # agent degrading.
    llm = ResilientLLM(llm)

    projected = project(live, scopes=SCOPES, wrap=wrap)  # type: ignore[arg-type]
    async with (
        connect(projected, ledger=InMemoryLedger()) as tools,
        # The approval workflow runs on Temporal's test server, on this
        # scenario's clock, and acts on the same projected world (T-028).
        durable.approvals_for(tools, clock=clock) as waits,
    ):
        approvals, escalations = waits.approvals, waits.escalations
        agent = ep.build(
            llm=llm,
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            escalations=escalations,
            clock=clock,
            config=config,
            metering=metering,
        )

        async def say(
            text: str, customer_id: str, conversation: object
        ) -> tuple[str, Conversation]:
            """The customer speaks and reads a reply. Which typed result produced
            that reply is this implementation's business, not the scenario's."""
            who = Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)
            held = conversation if isinstance(conversation, Conversation) else None
            result, held = await agent.handle(text, identity=who, conversation=held)
            reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
            return reply, held

        def reviewer(decision: str, by: str, delay_s: int = 0) -> Approver:
            deciding = durable.decide(waits.desk)
            return DECISIONS[decision](approvals, deciding, name=by, delay_s=delay_s)

        def colleague(resolution: str, by: str, delay_s: int = 0) -> Desk:
            resolving = durable.resolving(waits.colleagues)
            return RESOLUTIONS[resolution](escalations, resolving, name=by, delay_s=delay_s)

        async def opens(customer_id: str) -> str:
            who = Identity(customer_id=customer_id, scopes=ident.CUSTOMER_SCOPES)
            return await agent.opening(who)

        yield Subject(say=say, reviewer=reviewer, colleague=colleague, opens=opens)


def voice_of(client: LLMClient) -> Callable[[str, str], Awaitable[str]]:
    """A customer played by a model — the far side of AgentTwin's actor seam.

    Here rather than there for the same reason everything else is: that package
    cannot see a provider adapter, and one that imported this stack would
    simulate this stack. It is given a brief and what it last heard, and returns
    what the customer types.

    The customer's model is **not** the agent's model in any meaningful sense —
    it happens to be the same client here because there is only one provider
    configured, and a report that used the agent's own model to judge it would
    be worth nothing. It is used to *speak*, never to grade.

    Behind `ResilientLLM`, like the agent's own calls. Unwrapped, a provider rate
    limit on the *customer's* turn raised out of the run and the scenario was
    reported as crashed, which says nothing about the agent: live, 17 Sep, on
    Groq's free tier (T-050).
    """
    client = ResilientLLM(client)

    async def speak(brief: str, heard: str) -> str:
        opening = "Open the conversation — say what you want, in your own words."
        messages = (
            Message(role="system", content=brief, provenance="operator"),
            Message(role="user", content=heard or opening, provenance="user"),
        )
        # Generous on purpose. A reasoning model spends this budget thinking
        # before it writes anything, and a small one returns empty content after
        # paying for it — which is F-031 from the other side: the first version
        # of this asked for 120 tokens and got silence, three runs in a row, and
        # every check passed because a conversation that never happened cannot
        # say anything wrong.
        answer = await client.complete(ModelRequest(messages=messages, max_tokens=2048))
        return answer.text

    return speak


__all__ = ["subject_for", "voice_of"]
