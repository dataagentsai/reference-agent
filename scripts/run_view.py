"""Watch the runs, top to bottom, in a browser.

Every other artifact here answers *did it hold*. This one answers **what
happened** — the customer's words, the route the request took, what was sent to
the model and what came back, every tool call and the world's verdict on it, what
the people offstage did, and what the scenario checked at the end.

Generated from real runs, never drawn: a picture of an architecture is out of
date the first time somebody edits it, and this one cannot be, because it is
produced by running the thing.

    uv run python scripts/run_view.py                       # every scenario, offline
    uv run python scripts/run_view.py --scenario nobody-comes
    uv run python scripts/run_view.py --scenario nobody-comes --live

Three questions it is built to answer, because each was asked of the first
version and could not be answered from it:

* **Which tools does the model actually get, and does that change?** Every model
  call lists them, with where each one comes from. The answer is that the list is
  fixed for the whole run — `list_tools` is called once — and a reader can see
  that by comparing two calls rather than being told.
* **Where does each identifier come from?** An order id is the customer's, read
  out of the model's own tool arguments. An approval id is minted by the harness.
  Both are collected as they appear, with the place that produced them.
* **Who wrote this sentence — the model, or the code?** Every reply is looked up
  against the module-level constants in `src/`, so a reply the agent authored is
  labelled with the constant and the file it lives in. That distinction is the
  whole design of the approval path, and the first version of this page hid it.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agenttwin import (  # noqa: E402
    Clock,
    Live,
    load,
    load_scenario,
    perturbed,
    project,
    timeline_for,
)
from agenttwin.record import diff  # noqa: E402
from evals.simulation import DECISIONS, RESOLUTIONS  # noqa: E402
from scripts.run_view_html import render  # noqa: E402

from support_agent import approvals as ap  # noqa: E402
from support_agent import entrypoint as ep  # noqa: E402
from support_agent import escalation as esc  # noqa: E402
from support_agent import identity as ident  # noqa: E402
from support_agent import router  # noqa: E402
from support_agent.binding import SCOPES  # noqa: E402
from support_agent.config import Settings, resolve  # noqa: E402
from support_agent.contracts import Identity  # noqa: E402
from support_agent.idempotency import InMemoryLedger  # noqa: E402
from support_agent.llm import GroqClient  # noqa: E402
from support_agent.resilience import ResilientLLM  # noqa: E402
from support_agent.state import InMemoryCheckpointStore  # noqa: E402
from support_agent.tools import connect  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEST = ROOT / "docs" / "RUN-VIEW.html"
SRC = ROOT / "src" / "support_agent"


@dataclass
class Event:
    kind: str
    title: str
    detail: str = ""
    body: list[tuple[str, str]] = field(default_factory=list)
    verdict: str = ""
    note: str = ""
    """A line under the title that says where something came from — the constant
    a reply was read out of, the file an id was minted in."""


@dataclass
class Run:
    """One scenario, captured. Everything the page shows about it is here, so
    rendering is a pure function of this and cannot quietly do its own work."""

    name: str
    title: str
    objective: str
    world: str
    discharges: tuple[str, ...]
    events: list[Event] = field(default_factory=list)
    identifiers: list[tuple[str, str, str]] = field(default_factory=list)
    """(id, what it is, where it came from) — in the order they first appeared."""

    def add(self, kind: str, title: str, detail: str = "", body=None, verdict="", note="") -> None:
        self.events.append(Event(kind, title, detail, list(body or []), verdict, note))

    def saw(self, value: str, what: str, whence: str) -> None:
        if value and not any(value == seen for seen, _, _ in self.identifiers):
            self.identifiers.append((value, what, whence))

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    @property
    def checks(self) -> tuple[int, int]:
        outcomes = [e.verdict for e in self.events if e.kind == "check" and e.verdict]
        return sum(1 for v in outcomes if v == "passed"), len(outcomes)


def authored() -> dict[str, str]:
    """Every module-level string constant in the agent, by its value.

    So a reply can be attributed. `"I have sent this to a colleague to authorise"`
    is not the model being tactful — it is `approvals/refund.py:REFUND_WAIT_REPLY`,
    and the distinction is the entire reason the model is not allowed to phrase
    the wait. Read from the source rather than listed here, because a list of
    constants goes stale and a parse cannot.
    """
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        for node in ast.parse(path.read_text()).body:
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            value = getattr(node, "value", None)
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and len(value.value) > 12:
                    found.setdefault(value.value, f"{module}:{target.id}")
    return found


CONSTANTS = authored()


def wrote(reply: str) -> str:
    """Who authored this sentence. Templates are matched on their fixed half, so
    `RAISED_REPLY.format(ticket=...)` is still recognised as the agent's words."""
    if reply in CONSTANTS:
        return f"written by the agent — {CONSTANTS[reply]}"
    for text, where in CONSTANTS.items():
        head = text.split("{", 1)[0].strip()
        if len(head) > 20 and reply.startswith(head):
            return f"written by the agent — {where}, filled in"
    return "the model's own words"


class SeenLLM:
    """Wraps the model client and keeps every exchange. The wrapped client sees
    exactly what it would have seen — a recorder that changed the request would
    be showing a run that did not happen."""

    def __init__(self, inner, run: Run, local: frozenset[str]) -> None:
        self.inner, self.run, self.local, self.calls = inner, run, local, 0

    async def complete(self, request):
        self.calls += 1
        sent = [
            (m.role, m.content[:1400] + ("…" if len(m.content) > 1400 else ""))
            for m in request.messages
        ]
        offered = [t["function"]["name"] for t in request.tools]
        started = time.perf_counter()
        answer = await self.inner.complete(request)
        took = (time.perf_counter() - started) * 1000

        got: list[tuple[str, str]] = []
        if answer.text:
            got.append(("it answered in words", answer.text))
        for call in answer.tool_calls:
            got.append((f"it called {call.name}", repr(call.arguments)))
            for name, value in call.arguments.items():
                if isinstance(value, str) and value:
                    self.run.saw(
                        value,
                        f"{name}, as the model passed it",
                        "the customer's message, extracted by the model",
                    )
        self.run.add(
            "model",
            f"model call {self.calls}",
            f"{len(request.messages)} messages · {len(offered)} tools offered · "
            f"{answer.usage.input_tokens}+{answer.usage.output_tokens} tokens · {took:.0f} ms",
            [
                ("tools offered", self._tools(offered)),
                ("— sent —", ""),
                *sent,
                ("— came back —", ""),
                *(got or [("", "(nothing)")]),
            ],
            note="the same list every call: the tool surface is read once per run",
        )
        return answer

    def _tools(self, offered: list[str]) -> str:
        """Each tool with where it comes from — the world's projected surface, or
        the harness's own. The split is the answer to "what can this model do",
        and it is invisible in a bare list of names."""
        return "\n".join(
            f"{name}  — {'harness-local' if name in self.local else 'projected from the world'}"
            for name in offered
        )


def watching_tools(run: Run):
    """Wrap each projected tool so the call and the world's verdict are visible."""

    def wrap(tool: str, handler):
        async def wrapped(**arguments):
            result = await handler(**arguments)
            allowed = result.get("allowed")
            run.add(
                "tool",
                tool,
                ", ".join(f"{k}={v!r}" for k, v in arguments.items() if k != "ctx"),
                [("the system answered", repr(result)[:700])],
                verdict="refused" if allowed is False else "allowed" if allowed else "read",
            )
            return result

        wrapped.__name__ = handler.__name__
        wrapped.__doc__ = handler.__doc__
        wrapped.__signature__ = handler.__signature__  # type: ignore[attr-defined]
        wrapped.__annotations__ = handler.__annotations__
        return wrapped

    return wrap


class WatchedApprovals(ap.InMemoryApprovalStore):
    """Records the identifier at the moment it is minted, and where.

    An approval id is the one identifier in a run that nothing outside the
    harness could have supplied, which is what makes it the answer to *"which
    decision is this?"* a day later when a reviewer opens the queue.
    """

    def __init__(self, run: Run) -> None:
        super().__init__()
        self.run = run

    async def put(self, approval):
        self.run.saw(
            approval.id,
            f"approval for {approval.action}",
            "minted by the harness — approvals/workflow.py:request",
        )
        return await super().put(approval)


class WatchedEscalations(esc.InMemoryEscalationStore):
    def __init__(self, run: Run) -> None:
        super().__init__()
        self.run = run

    async def put(self, record):
        self.run.saw(
            record.id,
            f"escalation, rule {record.rule_id or '—'}",
            "minted by the harness — escalation/workflow.py:raise_for",
        )
        return await super().put(record)


async def capture(name: str, live_model: bool) -> Run:
    path = ROOT / "scenarios" / f"{name}.yaml"
    scenario = load_scenario(path)
    world = Live.start(load(path.parent / scenario.world))
    run = Run(
        name=name,
        title=scenario.scenario,
        objective=scenario.objective.strip(),
        world=world.world.name,
        discharges=tuple(scenario.discharges),
    )

    inner, config = _model(name, scenario, live_model)
    approvals, escalations = WatchedApprovals(run), WatchedEscalations(run)
    clock = Clock(step_s=scenario.step_seconds)
    local = frozenset({ap.REQUEST_REFUND})

    # The scenario's declared faults, applied exactly as the suite applies them.
    # Without this the page showed a different run from the one the suite
    # asserts: `stale-read-then-refused` never went stale, so four checks read as
    # failures here and pass there. A view that disagrees with the suite is worse
    # than no view, so the two now share the wrapper.
    timeline = timeline_for(scenario)
    faults = perturbed(world, timeline)
    watch = watching_tools(run)

    def wrap(tool: str, handler):
        return watch(tool, faults(tool, handler))

    async with connect(project(world, scopes=SCOPES, wrap=wrap), ledger=InMemoryLedger()) as tools:
        agent = ep.build(
            llm=SeenLLM(ResilientLLM(inner), run, local),
            tools=tools,
            store=InMemoryCheckpointStore(),
            approvals=approvals,
            escalations=escalations,
            clock=clock,
            config=config,
        )
        reviewer = (
            DECISIONS[scenario.approver.decides](approvals, ap.decide, name=scenario.approver.by)
            if scenario.approver
            else None
        )
        colleague = (
            RESOLUTIONS[scenario.desk.resolves](escalations, esc.resolve, name=scenario.desk.by)
            if scenario.desk
            else None
        )

        who = Identity(customer_id=scenario.as_, scopes=ident.CUSTOMER_SCOPES)
        run.saw(scenario.as_, "the customer", "the scenario — who is speaking")
        reply = await _talk(run, scenario, agent, world, who, clock, reviewer, colleague)

        # The same three inputs the suite evaluates against. An earlier version
        # passed nothing for the offstage records, so `handed_off` and `decided`
        # read zero and this page reported checks as failed that the suite passes
        # — a view that disagrees with the suite is worse than no view.
        offstage = {
            "handed": tuple(getattr(colleague, "handled", ()) or ()),
            "reviewed": tuple(getattr(reviewer, "reviewed", ()) or ()),
        }
        calls = dict(timeline.calls) if timeline is not None else {}
        world_0 = Live.start(load(path.parent / scenario.world)).snapshot()
        for check in scenario.expect:
            outcome = check.evaluate(world, world_0, reply, calls, offstage)
            run.add(
                "check",
                outcome.check,
                outcome.detail,
                verdict="passed" if outcome.passed else "failed",
            )
    return run


async def _talk(run, scenario, agent, world, who, clock, reviewer, colleague) -> str:
    """The conversation, turn by turn. Split out of `capture` when the statement
    ceiling refused it — which was the right call: opening the world and closing
    over the checks is one job, and what happens between is another."""
    conversation, before, reply = None, world.snapshot(), ""
    for said in scenario.actor.says[: scenario.max_turns]:
        run.add("customer", "the customer says", said)
        decision = router.route(said)
        run.add(
            "route",
            f"routed · {decision.kind}",
            router._reason_of(decision),
            verdict="no model call" if decision.kind != "agentic" else "to the loop",
        )
        result, conversation = await agent.handle(said, identity=who, conversation=conversation)
        reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
        run.add(
            "reply",
            f"the agent replies · {type(result).__name__}",
            reply,
            _result_body(result),
            note=wrote(reply),
        )
        if scenario.step_days:
            moved = world.advance(scenario.step_days)
            run.add(
                "world",
                f"{scenario.step_days} day(s) pass",
                f"{len(moved)} counter(s) moved — this is how a return window "
                f"closes while a customer is still talking",
                [("", field) for field in moved[:12]],
            )
        before = _world_moved(run, world, before)
        _offstage(run, await _reviews(clock, reviewer, colleague))
    return reply


def _model(name: str, scenario, live_model: bool):
    if live_model:
        settings = Settings(provider_api_key=os.environ.get("AGENT_PROVIDER_API_KEY", ""))
        config = resolve(settings)
        return GroqClient(
            api_key=settings.provider_api_key,
            base_url=config.provider_base_url,
            model=config.model,
            temperature=config.temperature,
        ), config
    from tests.test_scenario_files import model_for  # the suite's own scripts

    return model_for(name), None


def _result_body(result) -> list[tuple[str, str]]:
    """The fields of the result that are not its words. This is where an approval
    id or a ticket appears — the thing that makes a continuation real."""
    skip = {"reply", "customer_message", "kind"}
    return [
        (field_name, str(value))
        for field_name, value in result.model_dump().items()
        if field_name not in skip and value not in (None, "", 0)
    ]


def _world_moved(run: Run, world: Live, before):
    changed = diff(before, world.snapshot())
    if not changed:
        return before
    run.add(
        "world",
        "the world moved",
        "",
        [(f"{c.entity} {c.key}.{c.field}", f"{c.before!r} → {c.after!r}") for c in changed],
    )
    return world.snapshot()


async def _reviews(clock, reviewer, colleague):
    moment = clock.tick()
    out = []
    for who, actor in (("reviewer", reviewer), ("colleague", colleague)):
        if actor is not None:
            seen = await actor.review(at=moment)
            if seen:
                out.append((who, seen))
    return out


def _offstage(run: Run, reviews) -> None:
    for who, seen in reviews:
        run.add(
            "offstage",
            f"the {who} works the queue",
            "between turns, out of band — the agent is not running",
            [("", str(s)) for s in seen],
        )


async def main(only: str | None, live_model: bool) -> int:
    if live_model and not os.environ.get("AGENT_PROVIDER_API_KEY"):
        print("AGENT_PROVIDER_API_KEY is not set", file=sys.stderr)
        return 2
    names = [only] if only else sorted(p.stem for p in (ROOT / "scenarios").glob("*.yaml"))
    runs = []
    for name in names:
        try:
            runs.append(await capture(name, live_model))
        except Exception as exc:  # noqa: BLE001 — one bad scenario must not lose the rest
            print(f"  skipped {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    DEST.write_text(render(runs, live_model=live_model))
    steps = sum(len(r.events) for r in runs)
    print(f"{DEST}  {len(runs)} scenarios, {steps} steps")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=None, help="one scenario, by file stem")
    parser.add_argument("--live", action="store_true", help="use a real provider")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.scenario, args.live)))
