"""One conversation, showing both halves at once: what ran, and what it changed.

    uv run python scripts/trace_conversation.py [> trace.txt]

Two things are hard to hold in your head at the same time when reading this
system, and separately neither is enough.

*What ran* shows that the seven turns take four completely different paths —
a deterministic answer, the full loop, a refusal at the door, and a resume — and
that most turns never reach the model at all.

*What changed* shows where each turn's consequences landed: which of the five
stores gained a row, and whether the world moved.

Printed together, per turn, because the interesting questions are the ones that
span both. Why does turn 4 add nothing anywhere? Because it stopped at
`deliveries.claim`. Why does turn 7 write a ledger row without calling the
model? Because the decision was made two turns earlier and only the effect was
outstanding.

Everything below is the real system: a real MCP server projected from
`worlds/clothing.yaml`, the real router, loop, ledger, approvals and policy. Only
the model is scripted, so the run is free and identical every time.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import pathlib
import sys

# `agenttwin` sits at the repo root, outside the installed package, so a plain
# `uv run python scripts/...` cannot see it. Tests get it from pytest's
# pythonpath; this script has to say so itself.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agenttwin import Live, load, project
from evals import durable

from support_agent import context as ctx
from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import loop as agent_loop
from support_agent import policy as pol
from support_agent import router
from support_agent import telemetry as tel
from support_agent import tools as toolmod
from support_agent import trigger as trg
from support_agent.approvals import refund as refund_mod
from support_agent.approvals import workflow as approval_workflow
from support_agent.contracts import Identity, ModelResponse, ToolCall
from support_agent.entrypoint import direct
from support_agent.entrypoint import pending as pending_mod
from support_agent.idempotency import InMemoryLedger
from support_agent.llm import ScriptedClient
from support_agent.state import InMemoryCheckpointStore

WORLD = "worlds/clothing.yaml"
TRACE: list[str] = []
DEPTH = [0]
ON = [True]


RAISED: list[str] = []
"""Every approval this run raised, in order. Nothing lists them for us."""


def watch(obj: object, name: str, label: str, note: str = "") -> None:
    """Record every entry into one function, indented by call depth.

    Wrapping rather than `sys.settrace`, because a full trace of every frame
    buries the six or seven calls that carry the meaning under several hundred
    that do not.
    """
    fn = getattr(obj, name)
    line = lambda: TRACE.append(("  " * DEPTH[0]) + label + (f"   {note}" if note else ""))  # noqa: E731

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*a, **k):
            if not ON[0]:
                return await fn(*a, **k)
            line()
            DEPTH[0] += 1
            try:
                return await fn(*a, **k)
            finally:
                DEPTH[0] -= 1
    else:

        @functools.wraps(fn)
        def wrapper(*a, **k):
            if not ON[0]:
                return fn(*a, **k)
            line()
            DEPTH[0] += 1
            try:
                return fn(*a, **k)
            finally:
                DEPTH[0] -= 1

    setattr(obj, name, wrapper)


def calls(name: str, **arguments) -> ModelResponse:
    return ModelResponse(tool_calls=(ToolCall(id="c", name=name, arguments=arguments),))


TURNS = [
    ("where is my order AB-10003", "m1", "the router answers it — no AI call at all"),
    ("can I return my order AB-10003", "m2", "the full loop: ask, act, ask again, screen"),
    ("please cancel my order AB-10002", "m3", "same shape, but the effect cannot be undone"),
    ("please cancel my order AB-10002", "m3", "SAME delivery id — refused at the door"),
    (
        "I want my money back for AB-10003, it was 24000",
        "m5",
        "not returned yet, so a person decides — for the order's total, not the 24000 said",
    ),
    ("any update?", "m6", "resume: still waiting, and the router never runs"),
    ("any update?", "m7", "resume: the decision arrived, so the effect happens"),
]


async def main() -> None:
    exporter = tel.configure()
    world = Live.start(load(WORLD))
    store, ledger = InMemoryCheckpointStore(), InMemoryLedger()
    deliveries = trg.InMemoryDeliveryLog()
    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)

    llm = ScriptedClient(
        [
            calls("open_return_request", id="AB-10003"),
            ModelResponse(text="I have opened a return for AB-10003."),
            calls("cancel_order", id="AB-10002"),
            ModelResponse(text="That order has been cancelled."),
            calls("request_refund", order_id="AB-10003"),
        ]
    )

    async with (
        toolmod.connect(project(world), ledger=ledger) as tools,
        # The approval is a Temporal workflow (T-028), here on the test server.
        durable.approvals_for(tools) as waits,
    ):
        approvals = waits.approvals
        agent = ep.build(
            llm=llm, tools=tools, store=store, approvals=approvals, deliveries=deliveries
        )

        watch(agent, "handle", "entrypoint.handle", "the door")
        watch(trg, "once", "trigger.once", "have we seen this message?")
        watch(deliveries, "claim", "deliveries.claim")
        watch(agent, "_turn", "entrypoint._turn", "mint a run id")
        # Watched where each name is looked up at call time — a module that
        # imported a function by name holds its own reference to it.
        watch(pending_mod.ApprovalFlow, "resume", "pending.resume", "is an approval outstanding?")
        watch(router, "route", "router.route", "can we answer without the AI?")
        watch(direct, "answer", "direct.answer", "deterministic answer")
        watch(agent_loop, "run", "loop.run", "THE AI LOOP")
        watch(ctx, "assemble", "context.assemble", "build what the model sees")
        watch(llm, "complete", "llm.complete", "ask the model")
        watch(tools, "list_tools", "tools.list_tools", "filtered by this identity")
        watch(tools, "call", "tools.call", "run one tool")
        watch(ledger, "seen", "ledger.seen", "have we done this exact action?")
        watch(tools._transport, "invoke", "transport.invoke", "over MCP")
        watch(ledger, "record", "ledger.record", "write the action down")
        watch(pol, "enforce", "policy.enforce", "screen the reply before it is sent")
        watch(approvals, "request", "approvals.request", "raise it for a person")
        watch(refund_mod.RefundWork, "carry_out", "approvals.carry_out", "the workflow refunds")
        watch(
            approval_workflow,
            "granted_identity",
            "approvals.granted_identity",
            "borrow refunds:write",
        )
        watch(store, "checkpoint", "store.checkpoint", "save the conversation")

        conversation = None

        for n, (text, delivery, why) in enumerate(TURNS, 1):
            if n == 7:
                ON[0] = False
                waiting = (await approvals.pending())[0]
                RAISED.append(waiting.id)
                decided = await waits.desk.decide(waiting.id, granted=True, by="ops-7")
                ON[0] = True
                print(f"\n{'─' * 76}")
                print(f"  OUT OF BAND — a colleague opens the queue and grants {decided.id}")
                print("  Nobody is in a conversation. This happens hours later, elsewhere.")

            TRACE.clear()
            DEPTH[0] = 0
            print(f"\n{'═' * 76}")
            print(f'TURN {n}   "{text}"')
            print(f"{'─' * 76}\n  {why}\n")

            try:
                result, conversation = await agent.handle(
                    text, identity=who, conversation=conversation, delivery_id=delivery
                )
                reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
                outcome = type(result).__name__
            except trg.TriggerRefused as refused:
                reply, outcome = "", type(refused).__name__

            print("  WHAT RAN")
            for entry in TRACE:
                print("    " + entry)
            print(f'\n  REPLY  [{outcome}]\n    "{reply}"' if reply else f"\n  [{outcome}]")

            ON[0] = False
            await state(conversation, ledger, approvals, deliveries, world)
            ON[0] = True

        print(f"\n{'═' * 76}\nCOST OF THE WHOLE CONVERSATION\n{'─' * 76}")
        from collections import Counter

        seen = Counter(s.name for s in exporter.get_finished_spans())
        for name in sorted(seen):
            print(f"  {seen[name]:>3} x {name}")
        print(
            f"\n  {len(TURNS)} customer messages, {seen.get('agent.run', 0)} AI loops.\n"
            "  One answered by the router, one refused at the door, two resumed —\n"
            "  none of those four asked the model anything."
        )


async def state(conversation, ledger, approvals, deliveries, world) -> None:
    print("\n  WHAT CHANGED")
    if conversation is None:
        print("    conversation   (not started)")
    else:
        print(
            f"    conversation   {conversation.conversation_id}  "
            f"{len(conversation.messages)} messages  "
            f"pending_approval={conversation.pending_approval_id}"
        )
    entries = ledger._entries
    print(f"    ledger         {len(entries)} action(s) recorded")
    for key, value in entries.items():
        print(f"                     {key}  ->  {value.name}")
    # Read, never listed from inside: the approvals are a workflow's, and what
    # this process holds is a handle that can ask and look (T-028).
    raised = [await approvals.get(i) for i in RAISED]
    print(f"    approvals      {len(raised)}")
    for a in [a for a in raised if a is not None]:
        print(
            f"                     {a.id}  {a.action} {a.args.get('amount')}  "
            f"{a.state.value}  by={a.decided_by}  key={a.idempotency_key}"
        )
    print(f"    deliveries     {sorted(deliveries._seen)}")
    print(f"    world          {world.effects or '(nothing has changed)'}")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
