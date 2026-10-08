"""The numbers this agent is judged on, as counters rather than as spans.

Spans answer *what happened in this run*, and they answer it well. What they
cannot answer is *what is happening* — how often this agent refuses, how often
it fetches a person, what a resolved conversation costs — because every one of
those is an aggregate over runs, and computing an aggregate from a trace store
means either sampling (so the number is wrong in a way nobody can bound) or
scanning (so nobody runs it twice).

## Why these, and not everything on a span

A counter costs almost nothing to emit and a great deal to have: it is a series
somebody will alert on, put on a wall, and quote in a meeting, and every label on
it multiplies the storage of every other label. So this file holds exactly the
numbers this agent is *judged* on, each of which somebody would notice moving:

| Number | The question it answers |
|---|---|
| turns | how much traffic, and how it ends |
| escalations | how often a person is fetched, and by which rule |
| refusals | how often the agent says no, and under which rule |
| approvals | how often money waits on somebody, and what they decided |
| unbacked promises | how often the model writes a cheque the agent cannot cash |
| spend | what a unit of work costs — the only cost figure that is a business number |
| model calls | how many, and how many produced nothing readable |
| durations | how long a turn, a model call and a tool call took — p95, not the mean |
| tool calls | which tool, and whether it answered, refused, failed or was replayed |
| terminations | why runs stopped: the goal, a budget, going in circles |
| findings | what the online rules found in sampled turns (`watch`) |
| outcomes | what happened after a turn: the customer came back, gave feedback |
| canary | whether the synthetic customer's cases passed through the deployed edge |
| tools offered, truncated, invalid | what the model is shown, and where tool use goes wrong |

## Standard names where the standard has them

`gen_ai.client.operation.duration` and `gen_ai.client.token.usage` are the
OpenTelemetry GenAI semantic conventions' own client metrics, so any backend
that knows the conventions charts them with no mapping. Everything the
conventions cannot know — how a turn ended, which rule refused it — is
`agent.*`, as on the spans.

## Why the instruments are rebuilt

`bind` replaces every instrument with one from the given meter. The telemetry
module owns its provider for the same reason it owns its tracer provider: the
global is set-once, and a module built on it could not be reconfigured, so a
test could never read back what a turn recorded (AHC-0111). Callers look the
instruments up on this module at call time, so a rebind reaches all of them.

## Synthetic traffic

Every turn is labelled `synthetic` true or false, from a declared list of
synthetic customers (AHC-0113). Two values, and every alert rule filters on
`synthetic="false"`, so the canary never moves a business number.

Everything else stays a span attribute, where it is available when somebody is
investigating one run and costs nothing when nobody is.

## Labels are chosen to be small and to survive

Each label here has a bounded set of values known before the run: a rule id, a
termination reason, an outcome, a tool name. **Never a customer, never an order, never a free
string** — a label whose values come from the world is a series that grows
without limit, and the cardinality explosion is discovered by the bill.

`rule` is the deliberate exception worth noting: it is bounded by the rule set,
which is versioned configuration, so it grows only when somebody deliberately
adds a rule. That is the property that makes *which rule produces escalations a
human said were unnecessary* an answerable question, which is the question that
tunes the rule set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram, Meter

from agent_harness.contracts import Escalated, Refused, Route, TurnResult

METER_NAME = "support_agent"

_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120)
"""Bucket edges for every duration here. A turn with a model in it lives between
one and thirty seconds, and the default buckets (to 10 000) put all of it in two."""
_USD = (0.0001, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1)
_TOKENS = (16, 64, 256, 1024, 4096, 16384, 65536, 262144)

turns: Counter
turn_duration: Histogram
escalations: Counter
refusals: Counter
approvals: Counter
promises: Counter
spend: Histogram
model_calls: Counter
operation_duration: Histogram
token_usage: Histogram
tool_calls: Counter
tool_duration: Histogram
terminations: Counter
findings: Counter
scored: Counter
outcomes: Counter
canary: Counter
tools_offered: Counter
tool_truncated: Counter
tool_invalid: Counter

UNCONFIGURED = "none"
_CONFIG: ContextVar[str] = ContextVar("config", default=UNCONFIGURED)
"""The configuration fingerprint a turn runs under, as the `config` label on its
numbers (AACP-0002, AACP-0055). Bounded like `rule`: it changes only when a
release does, so it costs a series per release and makes *did this release move
the rates* a query rather than an argument. Set for exactly one turn by
`configured`, never left behind (F-084's lesson)."""


@contextmanager
def configured(fingerprint: str | None) -> Iterator[None]:
    """Label this turn's numbers with the configuration it runs under, and stop
    when the turn does."""
    token = _CONFIG.set(fingerprint or UNCONFIGURED)
    try:
        yield
    finally:
        _CONFIG.reset(token)


def configuration() -> str:
    return _CONFIG.get()


def bind(meter: Meter) -> None:
    """Build every instrument from `meter`, replacing whatever was there."""
    global turns, turn_duration, escalations, refusals, approvals, promises, spend
    global model_calls, operation_duration, token_usage, tool_calls, tool_duration
    global terminations, findings, scored, outcomes, canary

    turns = meter.create_counter("agent.turns", description="Turns served, by how each one ended.")
    turn_duration = meter.create_histogram(
        "agent.turn.duration",
        unit="s",
        description="How long a turn took, from the customer's words to the reply.",
        explicit_bucket_boundaries_advisory=_SECONDS,
    )
    escalations = meter.create_counter(
        "agent.escalations",
        description="Conversations handed to a person, by the rule that decided.",
    )
    refusals = meter.create_counter(
        "agent.refusals", description="Requests refused, by the rule that refused them."
    )
    approvals = meter.create_counter(
        "agent.approvals", description="Approval decisions, by what was decided."
    )
    promises = meter.create_counter(
        "agent.unbacked_promises",
        description="Replies that committed to work nothing was doing (AHC-0106).",
    )
    spend = meter.create_histogram(
        "agent.spend",
        unit="USD",
        description="What one run spent with the provider.",
        explicit_bucket_boundaries_advisory=_USD,
    )
    model_calls = meter.create_counter(
        "agent.model.calls",
        description="Calls to the provider, and how many came back unreadable.",
    )
    operation_duration = meter.create_histogram(
        "gen_ai.client.operation.duration",
        unit="s",
        description="GenAI semantic conventions: duration of a model call.",
        explicit_bucket_boundaries_advisory=_SECONDS,
    )
    token_usage = meter.create_histogram(
        "gen_ai.client.token.usage",
        unit="{token}",
        description="GenAI semantic conventions: tokens per model call, by type.",
        explicit_bucket_boundaries_advisory=_TOKENS,
    )
    tool_calls = meter.create_counter(
        "agent.tool.calls", description="Tool calls, by tool and by what came back."
    )
    tool_duration = meter.create_histogram(
        "agent.tool.duration",
        unit="s",
        description="How long a tool call took, measured at the agent.",
        explicit_bucket_boundaries_advisory=_SECONDS,
    )
    terminations = meter.create_counter(
        "agent.run.terminations", description="Why each run stopped."
    )
    findings = meter.create_counter(
        "agent.online.findings",
        description="What the online rules found in evaluated turns, by rule.",
    )
    scored = meter.create_counter(
        "agent.online.evaluated", description="Turns the online rules evaluated."
    )
    outcomes = meter.create_counter(
        "agent.outcomes",
        description="What happened after a turn (AHC-0112), by kind and source.",
    )
    canary = meter.create_counter(
        "agent.canary.cases",
        description="The synthetic customer's cases through the deployed edge (AHC-0113).",
    )
    _bind_tool_surface(meter)


def _bind_tool_surface(meter: Meter) -> None:
    """What the model is shown and where its tool use goes wrong — AACP-0026,
    0004, 0024. Apart from `bind` only to keep each function short."""
    global tools_offered, tool_truncated, tool_invalid
    tools_offered = meter.create_counter(
        "agent.tools.offered",
        description="Each tool on the surface, once per listing (AACP-0026).",
    )
    tool_truncated = meter.create_counter(
        "agent.tool.truncated",
        description="Tool results cut to fit the context, by tool (AACP-0004).",
    )
    tool_invalid = meter.create_counter(
        "agent.tool.invalid_arguments",
        description="Tool calls whose arguments failed the tool's schema, by tool (AACP-0024).",
    )


bind(metrics.get_meter(METER_NAME))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def record_turn(
    result: TurnResult, decision: Route, *, duration_s: float = 0.0, synthetic: bool = False
) -> None:
    """Every number a turn produces, at the one point every turn passes through.

    Here rather than at each branch, because a counter incremented in five
    places is a counter that will be missed in the sixth — and the rates share
    this denominator, so a turn counted differently from a refusal makes the
    refusal rate a ratio of two different things.
    """
    labels = {
        "result": result.kind,
        "route": decision.kind,
        "synthetic": _flag(synthetic),
        "config": configuration(),
    }
    turns.add(1, labels)
    turn_duration.record(duration_s, labels)
    match result:
        case Refused():
            refusals.add(
                1, {"rule": result.rule_id or "unattributed", "synthetic": _flag(synthetic)}
            )
        case Escalated():
            escalations.add(
                1,
                {
                    "rule": result.rule_id or "unattributed",
                    "tier": "1",
                    "synthetic": _flag(synthetic),
                },
            )
        case _:
            return


__all__ = [
    "METER_NAME",
    "UNCONFIGURED",
    "approvals",
    "bind",
    "canary",
    "configuration",
    "configured",
    "escalations",
    "findings",
    "model_calls",
    "operation_duration",
    "outcomes",
    "promises",
    "record_turn",
    "refusals",
    "scored",
    "spend",
    "terminations",
    "token_usage",
    "tool_calls",
    "tool_duration",
    "tool_invalid",
    "tool_truncated",
    "tools_offered",
    "turn_duration",
    "turns",
]
