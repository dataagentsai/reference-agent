"""Watch one run, top to bottom, in a browser.

Every other artifact here answers *did it hold*. This one answers **what
happened** — the customer's words, the route the request took, what was sent to
the model and what came back, every tool call and the world's verdict on it, what
the people offstage did, and what the scenario checked at the end.

Generated from a real run, never drawn: a picture of an architecture is out of
date the first time somebody edits it, and this one cannot be, because it is
produced by running the thing.

    uv run python scripts/run_view.py                    # offline, scripted model
    uv run python scripts/run_view.py --live             # a real provider call
    uv run python scripts/run_view.py --scenario nobody-comes

The left rail is the **position** each step happens at — the same P1…P8 the
harness catalog uses, so a reader can see which layer owns what.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agenttwin import Clock, Live, load, load_scenario, project  # noqa: E402
from agenttwin.record import diff  # noqa: E402

from evals.simulation import DECISIONS, RESOLUTIONS  # noqa: E402
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

POSITIONS = {
    "customer": ("P1", "edge"),
    "route": ("P3", "harness"),
    "model": ("P3", "harness"),
    "tool": ("P5", "tool boundary"),
    "world": ("P6", "state"),
    "offstage": ("P8", "human"),
    "reply": ("P3", "harness"),
    "check": ("P7", "offline"),
}


@dataclass
class Event:
    kind: str
    title: str
    detail: str = ""
    body: list[tuple[str, str]] = field(default_factory=list)
    verdict: str = ""


class Watched:
    """Collects what happened, in order. Nothing here changes behaviour."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def add(self, kind: str, title: str, detail: str = "", body=None, verdict: str = "") -> None:
        self.events.append(Event(kind, title, detail, list(body or []), verdict))


class SeenLLM:
    """Wraps the model client and keeps every exchange. The wrapped client sees
    exactly what it would have seen — a recorder that changed the request would
    be showing a run that did not happen."""

    def __init__(self, inner, watch: Watched) -> None:
        self.inner, self.watch, self.calls = inner, watch, 0

    async def complete(self, request):
        self.calls += 1
        sent = [
            (f"{m.role}", m.content[:1400] + ("…" if len(m.content) > 1400 else ""))
            for m in request.messages
        ]
        tools = ", ".join(t["function"]["name"] for t in request.tools) or "none"
        started = time.perf_counter()
        answer = await self.inner.complete(request)
        took = (time.perf_counter() - started) * 1000
        got = []
        if answer.text:
            got.append(("text", answer.text))
        for call in answer.tool_calls:
            got.append((f"calls {call.name}", json.dumps(call.arguments)))
        self.watch.add(
            "model",
            f"model call {self.calls}",
            f"{len(request.messages)} messages in · tools offered: {tools} · "
            f"{answer.usage.input_tokens}+{answer.usage.output_tokens} tokens · {took:.0f} ms",
            [("— sent —", "")] + sent + [("— came back —", "")] + (got or [("", "(nothing)")]),
        )
        return answer


def watching_tools(live: Live, watch: Watched):
    """Wrap each projected tool so the call and the world's verdict are visible."""

    def wrap(tool: str, handler):
        async def wrapped(**arguments):
            result = await handler(**arguments)
            allowed = result.get("allowed")
            watch.add(
                "tool",
                f"{tool}",
                json.dumps({k: v for k, v in arguments.items() if k != "ctx"}),
                [("the system answered", json.dumps(result, default=str)[:600])],
                verdict="refused" if allowed is False else "allowed" if allowed else "read",
            )
            return result

        wrapped.__name__ = handler.__name__
        wrapped.__doc__ = handler.__doc__
        wrapped.__signature__ = handler.__signature__  # type: ignore[attr-defined]
        wrapped.__annotations__ = handler.__annotations__
        return wrapped

    return wrap


async def run(scenario_name: str, live_model: bool) -> Watched:
    path = ROOT / "scenarios" / f"{scenario_name}.yaml"
    scenario = load_scenario(path)
    world = Live.start(load(path.parent / scenario.world))
    watch = Watched()
    watch.add(
        "check",
        f"scenario · {scenario.scenario}",
        f"world {world.world.name} · acting as {scenario.as_} · "
        f"{'a real model' if live_model else 'a scripted model'}",
        [("it is for", scenario.objective.strip())]
        + [("it discharges", ", ".join(scenario.discharges))],
    )

    if live_model:
        settings = Settings(provider_api_key=os.environ.get("AGENT_PROVIDER_API_KEY", ""))
        config = resolve(settings)
        inner = GroqClient(
            api_key=settings.provider_api_key,
            base_url=config.provider_base_url,
            model=config.model,
            temperature=config.temperature,
        )
    else:
        from tests.test_scenario_files import model_for  # the suite's scripts

        inner, config = model_for(scenario_name), None

    llm = SeenLLM(ResilientLLM(inner), watch)
    approvals, escalations = ap.InMemoryApprovalStore(), esc.InMemoryEscalationStore()
    clock = Clock(step_s=scenario.step_seconds)

    async with connect(
        project(world, scopes=SCOPES, wrap=watching_tools(world, watch)), ledger=InMemoryLedger()
    ) as tools:
        agent = ep.build(
            llm=llm,
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
        conversation, before = None, world.snapshot()
        for said in scenario.actor.says[: scenario.max_turns]:
            watch.add("customer", "the customer says", said)
            decision = router.route(said)
            watch.add(
                "route",
                f"routed · {decision.kind}",
                router._reason_of(decision),
                verdict="no model call" if decision.kind != "agentic" else "to the loop",
            )
            result, conversation = await agent.handle(said, identity=who, conversation=conversation)
            reply = getattr(result, "reply", "") or getattr(result, "customer_message", "")
            watch.add("reply", f"the agent replies · {type(result).__name__}", reply)

            changed = diff(before, world.snapshot())
            if changed:
                watch.add(
                    "world",
                    "the world moved",
                    "",
                    [
                        (f"{c.entity} {c.key}.{c.field}", f"{c.before!r} → {c.after!r}")
                        for c in changed
                    ],
                )
                before = world.snapshot()

            moment = clock.tick()
            for who_offstage, actor in (("reviewer", reviewer), ("colleague", colleague)):
                if actor is None:
                    continue
                seen = await actor.review(at=moment)
                if seen:
                    watch.add(
                        "offstage",
                        f"the {who_offstage} works the queue",
                        "between turns, out of band",
                        [("", str(s)) for s in seen],
                    )

        world_0 = Live.start(load(path.parent / scenario.world)).snapshot()
        for check in scenario.expect:
            outcome = check.evaluate(world, world_0, reply, {}, None)
            watch.add(
                "check",
                outcome.check,
                outcome.detail,
                verdict="passed" if outcome.passed else "failed",
            )
    return watch


STYLE = """
/* One token set, redefined for the two dark states — an explicit choice and an
   unstamped system preference — so the page holds on whatever ground it lands on. */
:root{--ink:#191b1f;--dim:#6a7180;--rule:#e3e5ea;--bg:#fafaf8;--card:#fff;
 --shade:#f4f5f7;--good:#15803d;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --ink:#e8e9ec;--dim:#9aa1b0;--rule:#2c2f37;--bg:#131519;--card:#1a1d22;
 --shade:#22262d;--good:#5fd08a;--bad:#f38b8b}}
:root[data-theme="dark"]{--ink:#e8e9ec;--dim:#9aa1b0;--rule:#2c2f37;--bg:#131519;
 --card:#1a1d22;--shade:#22262d;--good:#5fd08a;--bad:#f38b8b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:28px 32px;border-bottom:1px solid var(--rule);background:var(--card)}
h1{margin:0 0 6px;font-size:21px;letter-spacing:-.01em}
header p{margin:0;color:var(--dim);max-width:70ch}
main{max-width:1000px;margin:0 auto;padding:24px 20px 80px}
.row{display:grid;grid-template-columns:96px 1fr;gap:16px;margin:0 0 2px}
.rail{text-align:right;padding-top:14px;color:var(--dim);font-size:11px;
 text-transform:uppercase;letter-spacing:.06em;border-right:1px solid var(--rule);padding-right:12px}
.rail b{display:block;color:var(--ink);font-size:12px;letter-spacing:0}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;
 padding:12px 16px;margin:6px 0}
.card h2{margin:0;font-size:14px;font-weight:600;display:flex;gap:8px;align-items:center}
/* The badge colours are the one place hue carries meaning: which layer acted.
   Set on the shared surface token so they read on either ground. */
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.08em;padding:2px 8px;
 border-radius:3px;background:var(--shade);color:var(--dim);font-weight:600}
.tag.tool{color:#0f766e}.tag.model{color:#a16207}.tag.customer{color:#475569}
.tag.world{color:#86198f}.tag.offstage{color:#be123c}.tag.check{color:#0369a1}
.tag.reply{color:#15803d}.tag.route{color:#4338ca}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .tag.tool{color:#5eead4}
 :root:not([data-theme="light"]) .tag.model{color:#fcd34d}
 :root:not([data-theme="light"]) .tag.world{color:#f0abfc}
 :root:not([data-theme="light"]) .tag.offstage{color:#fda4af}
 :root:not([data-theme="light"]) .tag.check{color:#7dd3fc}
 :root:not([data-theme="light"]) .tag.reply{color:#86efac}
 :root:not([data-theme="light"]) .tag.route{color:#a5b4fc}
 :root:not([data-theme="light"]) .tag.customer{color:#cbd5e1}}
.said{margin:6px 0 0;white-space:pre-wrap}
.detail{color:var(--dim);font-size:13px;margin:4px 0 0}
.verdict{margin-left:auto;font-size:11px;font-weight:600;letter-spacing:.04em}
.ok{color:var(--good)}.no{color:var(--bad)}.neutral{color:var(--dim)}
details{margin:8px 0 0}summary{cursor:pointer;color:var(--dim);font-size:12px}
table{border-collapse:collapse;width:100%;margin:8px 0 0;font-size:13px}
td{border-top:1px solid var(--rule);padding:6px 8px;vertical-align:top}
td:first-child{color:var(--dim);white-space:nowrap;width:1%;font-size:12px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,monospace}
.legend{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px;font-size:12px;color:var(--dim)}
"""

LEGEND = [
    ("customer", "what the person typed"),
    ("route", "which path it took — three of four never reach a model"),
    ("model", "what was sent and what came back"),
    ("tool", "an action, and the world's verdict on it"),
    ("world", "what actually changed"),
    ("offstage", "a reviewer or colleague, between turns"),
    ("reply", "what the customer reads"),
    ("check", "what the scenario asserted"),
]


def render(watch: Watched, scenario_name: str) -> str:
    e = html.escape
    rows = []
    for event in watch.events:
        position, layer = POSITIONS.get(event.kind, ("", ""))
        verdict_class = (
            "ok"
            if event.verdict in {"passed", "allowed"}
            else "no"
            if event.verdict in {"failed", "refused"}
            else "neutral"
        )
        body = ""
        if event.body:
            cells = "".join(
                f"<tr><td>{e(label)}</td><td><pre>{e(str(value))}</pre></td></tr>"
                for label, value in event.body
            )
            body = (
                f"<details><summary>what passed through</summary><table>{cells}</table></details>"
            )
        rows.append(
            f"""<div class="row"><div class="rail"><b>{position}</b>{e(layer)}</div>
<div class="card"><h2><span class="tag {event.kind}">{e(event.kind)}</span>{e(event.title)}
{f'<span class="verdict {verdict_class}">{e(event.verdict)}</span>' if event.verdict else ""}</h2>
{f'<p class="said">{e(event.detail)}</p>' if event.detail else ""}{body}</div></div>"""
        )

    legend = "".join(
        f'<span><span class="tag {kind}">{kind}</span> {e(what)}</span>' for kind, what in LEGEND
    )
    return f"""<title>Support Agent Run Trace</title>
<style>{STYLE}</style>
<header>
<h1>One run, top to bottom</h1>
<p>Generated by <code>scripts/run_view.py</code> from an actual run — never drawn. The left
rail is the position each step happens at, the same P1–P8 the harness catalog uses, so you
can see which layer owns what. Open <em>what passed through</em> on any step to see the
data itself.</p>
</header>
<main>
<div class="legend">{legend}</div>
{"".join(rows)}
</main>"""


async def main(scenario_name: str, live_model: bool) -> int:
    if live_model and not os.environ.get("AGENT_PROVIDER_API_KEY"):
        print("AGENT_PROVIDER_API_KEY is not set", file=sys.stderr)
        return 2
    watch = await run(scenario_name, live_model)
    DEST.write_text(render(watch, scenario_name))
    print(f"{DEST}  {len(watch.events)} steps")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="refund-needs-a-person")
    parser.add_argument("--live", action="store_true", help="use a real provider")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.scenario, args.live)))
