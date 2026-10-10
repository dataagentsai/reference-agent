"""Tier 2b — checks as plug-ins (architecture deck, slides 93–94).

One port (`agent_harness.evals.Evaluator`), one request, one result; where each
check runs is `evaluators.yaml`. Tables:

1. Startup refuses a wrong YAML, naming what is wrong.
2. A request lacking what an evaluator needs is `skip`, never `fail`.
3. The `reply` position, placed by YAML, gives the same verdict as the rule
   lists it replaced, on every route — output identical.
4. Every configured evaluator runs on one golden case.
5. Moving a check or changing a threshold is a YAML-only change.
6. Release gates and online sampling.
"""

from __future__ import annotations

import pytest
import yaml

from agent_harness.contracts import Identity, ToolCall, ToolResult
from agent_harness.evals import EvalRequest, Expected, Meta, Response, judge
from agent_harness.evals import plan as ev
from agent_harness.evals.rule import RuleSpec
from agent_harness.policy import Context, Position, enforce
from agent_harness.watch import online
from agent_harness.watch.record import ToolUse, Turn
from support_agent import policy as pol

YAML = pol.EVALUATORS.read_text()

BEFORE_OUTPUT = (
    pol.no_unclaimed_effect,
    pol.no_ungrounded_entity,
    pol.no_superseded_state,
    pol.no_invented_delivery_date,
    pol.no_pii_echo,
    pol.no_discount_offer,
)
BEFORE_REPLY = (pol.no_invented_delivery_date, pol.no_pii_echo, pol.no_discount_offer)
"""The hard-coded lists `evaluators.yaml` replaced, kept here as the yardstick."""


def plan_of(text: str, rules: dict[str, RuleSpec] | None = None) -> ev.Plan:
    return ev.load(text, rules=pol.RULES if rules is None else rules)


def edited(change: object) -> str:
    """This agent's YAML with one edit applied — the only thing a move takes."""
    document = yaml.safe_load(YAML)
    change(document)  # type: ignore[operator]
    return yaml.safe_dump(document)


def placing(name: str, position: str, extra: str = "") -> str:
    """A one-evaluator YAML: `name`, a rule, at `position`, with `extra` on its entry."""
    entry = f"{{use: {name}{', ' + extra if extra else ''}}}"
    where = f"[{entry}]" if position == "reply" else f"{{run: [{entry}]}}"
    return f"evaluators: {{{name}: {{kind: rule}}}}\npositions: {{{position}: {where}}}"


# --------------------------------------------------------------------------- 1
REFUSALS: list[tuple[str, str, str]] = [
    (
        "unknown kind",
        "evaluators: {x: {kind: crystal_ball}}",
        "has no 'crystal_ball'; known: azure",
    ),
    (
        "unknown field on a kind",
        "evaluators: {no_pii_echo: {kind: rule, colour: red}}",
        "unknown field 'colour'",
    ),
    ("unknown top-level field", "evaluator: {}", "unknown field 'evaluator'"),
    ("unknown position", "positions: {nightly: {run: []}}", "unknown field 'nightly'"),
    ("a rule not in the catalogue", "evaluators: {no_magic: {kind: rule}}", "no rule 'no_magic'"),
    ("an evaluator with no kind", "evaluators: {no_pii_echo: {}}", "needs a `kind`"),
    (
        "placing an undeclared name",
        "positions: {reply: [{use: no_pii_echo}]}",
        "'no_pii_echo' is not declared",
    ),
    (
        "an unknown field on an entry",
        placing("no_pii_echo", "reply", "when: always"),
        "unknown field 'when'",
    ),
    (
        "an on_fail the position does not have",
        placing("no_pii_echo", "reply", "on_fail: block"),
        "on_fail 'block'; allowed: safe_reply, alert",
    ),
    (
        "a check too slow for its max_ms at reply",
        placing("no_pii_echo", "reply", "max_ms: 1"),
        "reply allows inline evaluators within 1 ms",
    ),
    (
        "tool checks where there are no tool definitions or expectations",
        placing("tool_selection", "reply"),
        "needs expected, tool_calls, which reply cannot provide",
    ),
    (
        "a golden-only check online",
        placing("must_include", "online"),
        "needs expected, which online cannot provide",
    ),
    (
        "a release minimum that is not a pass rate",
        placing("must_include", "release", "min: baseline"),
        "min 'baseline': a pass rate between 0 and 1",
    ),
]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize(("name", "text", "says"), REFUSALS, ids=[r[0] for r in REFUSALS])
def test_startup_refuses_a_wrong_plan(name: str, text: str, says: str) -> None:
    with pytest.raises(ev.PlanRefused, match=None) as refused:
        plan_of(text)
    assert says in str(refused.value)


STUBS = ["presidio", "azure", "open_model"]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize("kind", STUBS)
def test_a_stub_kind_refuses_only_when_a_yaml_uses_it(kind: str) -> None:
    assert plan_of(YAML).evaluators  # registered, unused: starts
    with pytest.raises(ev.NotBuilt, match=r"not built yet \(Tier 3/12\)"):
        plan_of(f"evaluators: {{probe: {{kind: {kind}}}}}")


# --------------------------------------------------------------------------- 2
REPLY_ONLY = EvalRequest(response=Response(text="Your order AB-10010 is on its way."))
WITH_TOOLS = EvalRequest(
    response=Response(
        text="Your order AB-10010 is on its way.",
        tool_calls=(ToolCall(id="c1", name="get_order", arguments={"id": "AB-10010"}),),
        tool_results=(ToolResult(name="get_order", structured={"id": "AB-10010"}, text=""),),
    ),
    expected=Expected(must_call=("get_order",)),
)
SKIPS: list[tuple[str, str, EvalRequest, str]] = [
    ("grounding with no tool results recorded", "no_ungrounded_entity", REPLY_ONLY, "skip"),
    ("grounding with them", "no_ungrounded_entity", WITH_TOOLS, "pass"),
    ("tool selection outside a golden case", "tool_selection", REPLY_ONLY, "skip"),
    ("tool selection on one", "tool_selection", WITH_TOOLS, "pass"),
    ("a reply rule on the reply alone", "no_pii_echo", REPLY_ONLY, "pass"),
]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize(
    ("name", "evaluator", "request_", "verdict"), SKIPS, ids=[r[0] for r in SKIPS]
)
def test_missing_input_is_a_skip(
    name: str, evaluator: str, request_: EvalRequest, verdict: str
) -> None:
    result = judge(plan_of(YAML).evaluators[evaluator], request_)
    assert result.verdict == verdict
    if verdict == "skip":
        assert result.reason.startswith("skip: missing ")
        assert result.score is None


# --------------------------------------------------------------------------- 3
ORDER = ToolResult(name="get_order", structured={"id": "AB-10010", "status": "shipped"}, text="")
REFUNDED = ToolResult(name="issue_refund", structured={"id": "AB-10010", "allowed": True}, text="")
CASES: list[tuple[str, str, tuple[ToolResult, ...]]] = [
    ("a grounded status", "Your order AB-10010 has shipped.", (ORDER,)),
    ("an invented order", "Your order AB-99999 has shipped.", (ORDER,)),
    ("a refund claimed, not made", "I've refunded your order AB-10010.", (ORDER,)),
    ("a refund claimed and made", "I've refunded your order AB-10010.", (ORDER, REFUNDED)),
    ("a superseded state", "Your order AB-10010 is delivered.", (ORDER,)),
    ("an invented delivery date", "It will arrive on 2026-12-01.", ()),
    ("a card number echoed", "Your card 4111111111111111 is on file.", ()),
    ("a discount volunteered", "Here is 10% off your next order.", ()),
    ("an unsupported figure", "You will get 4,999 back.", (ORDER,)),
    ("a plain reply", "Could you give me the order number?", ()),
]


@pytest.mark.discharges("AHC-0094", "AHC-0028")
@pytest.mark.parametrize(("name", "text", "results"), CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize(
    ("position", "before", "after"),
    [
        (Position.POST_MODEL, BEFORE_OUTPUT, pol.OUTPUT_RULES),
        (Position.REPLY, BEFORE_REPLY, pol.REPLY_RULES),
    ],
    ids=["after the model", "every route"],
)
def test_the_yaml_placed_rules_judge_as_the_lists_did(
    name: str,
    text: str,
    results: tuple[ToolResult, ...],
    position: Position,
    before: tuple,
    after: tuple,
) -> None:
    given = results if position is Position.POST_MODEL else ()
    ctx = Context(
        position=position, identity=Identity(customer_id="c-1"), text=text, tool_results=given
    )
    assert enforce(ctx, after) == enforce(ctx, before)
    assert pol.DEFAULT_RULES[position] == after


def broken(_: Context) -> object:
    raise RuntimeError("regex engine fell over")


@pytest.mark.discharges("AAC-0091")
def test_an_erroring_inline_check_fails_closed_as_a_rule_did() -> None:
    rules = {"broken": RuleSpec(broken)}  # type: ignore[arg-type]
    placed = ev.load(
        "evaluators: {broken: {kind: rule}}\npositions: {reply: [{use: broken}]}", rules=rules
    )
    ctx = Context(position=Position.POST_MODEL, identity=Identity(customer_id="c-1"), text="hi")
    assert enforce(ctx, placed.inline("model")) == enforce(ctx, (broken,))  # type: ignore[arg-type]
    assert enforce(ctx, placed.inline("model")).reason.startswith(
        "rule failed and traffic was blocked"
    )


# --------------------------------------------------------------------------- 4
GOLDEN = EvalRequest(
    query="Where is my order AB-10010?",
    messages=({"role": "user", "content": "Where is my order AB-10010?"},),
    response=WITH_TOOLS.response,
    tool_definitions=({"type": "function", "function": {"name": "get_order", "parameters": {}}},),
    context=(),
    expected=Expected(route="agent", must_call=("get_order",), must_include=("AB-10010",)),
    meta=Meta(agent="support-agent", trace="golden-1", position="release"),
)


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize("name", sorted(plan_of(YAML).evaluators))
def test_every_configured_evaluator_runs_on_a_golden_case(name: str) -> None:
    evaluator = plan_of(YAML).evaluators[name]
    result = judge(evaluator, GOLDEN)
    assert (result.verdict, result.evaluator, result.provider) == ("pass", name, "ours")
    assert (result.score, result.threshold, result.cost) == (1.0, 1.0, 0.0)
    assert result.latency_ms >= 0 and result.version


# --------------------------------------------------------------------------- 5
def _move_pii_online(doc: dict) -> None:
    doc["positions"]["reply"] = [e for e in doc["positions"]["reply"] if e["use"] != "no_pii_echo"]


def _threshold_zero(doc: dict) -> None:
    doc["evaluators"]["no_discount_offer"]["threshold"] = 0.0


def _alert_only(doc: dict) -> None:
    for entry in doc["positions"]["reply"]:
        if entry["use"] == "no_discount_offer":
            entry["on_fail"] = "alert"


CARD = "Your card 4111111111111111 is on file."
DISCOUNT = "Here is 10% off your next order."
MOVES: list[tuple[str, object, str, bool]] = [
    ("as shipped, a card number is blocked", lambda d: None, CARD, True),
    ("taken off reply, it is not", _move_pii_online, CARD, False),
    ("as shipped, a discount is blocked", lambda d: None, DISCOUNT, True),
    ("a threshold of 0 lets it pass", _threshold_zero, DISCOUNT, False),
    ("on_fail alert lets it pass inline", _alert_only, DISCOUNT, False),
]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize(("name", "change", "text", "blocked"), MOVES, ids=[m[0] for m in MOVES])
def test_moving_a_check_is_a_yaml_change(
    name: str, change: object, text: str, blocked: bool
) -> None:
    moved = plan_of(edited(change))
    ctx = Context(position=Position.REPLY, identity=Identity(customer_id="c-1"), text=text)
    assert enforce(ctx, moved.inline("every_route")).blocked is blocked


# --------------------------------------------------------------------------- 6
def _bad_golden(called: str) -> EvalRequest:
    return EvalRequest(
        response=Response(
            text="AB-10010", tool_calls=(ToolCall(id="c", name=called, arguments={}),)
        ),
        expected=Expected(must_call=("get_order",)),
    )


GATES: list[tuple[str, list[EvalRequest], float, bool]] = [
    ("all pass", [GOLDEN, GOLDEN], 1.0, False),
    ("one of two misses its tool", [GOLDEN, _bad_golden("list_orders")], 1.0, True),
    ("the same, with min lowered in YAML", [GOLDEN, _bad_golden("list_orders")], 0.5, False),
    ("nothing judged: no golden cases", [REPLY_ONLY], 1.0, False),
]


@pytest.mark.discharges("AHC-0028")
@pytest.mark.parametrize(("name", "batch", "minimum", "blocks"), GATES, ids=[g[0] for g in GATES])
def test_the_release_gate(
    name: str, batch: list[EvalRequest], minimum: float, blocks: bool
) -> None:
    text = placing("tool_selection", "release", f"min: {minimum}")
    (gate,), _ = plan_of(text).run_release(batch)
    assert gate.blocks is blocks


SAMPLES: list[tuple[str, float, int]] = [("everything", 1.0, 200), ("nothing", 0.0, 0)]


@pytest.mark.discharges("AAC-0014")
@pytest.mark.parametrize(("name", "rate", "expected"), SAMPLES, ids=[s[0] for s in SAMPLES])
def test_online_sampling_is_by_rate_and_repeatable(name: str, rate: float, expected: int) -> None:
    online = plan_of(edited(lambda d: d["positions"]["online"].update(sample=rate)))
    keys = [f"trace-{i}" for i in range(100)]
    first = [len(online.run_online(REPLY_ONLY, key=k)) for k in keys]
    assert sum(first) == expected
    assert first == [len(online.run_online(REPLY_ONLY, key=k)) for k in keys]


@pytest.mark.discharges("AHC-0028")
def test_the_shipped_yaml_places_every_catalogued_rule() -> None:
    placed = plan_of(YAML)
    assert set(pol.RULES) <= set(placed.evaluators)
    assert all(placed.where(name) for name in pol.RULES)


# --------------------------------------------------------------------------- 7
def _turn(**changes: object) -> Turn:
    base: dict[str, object] = dict(
        trace_id="t-1",
        run_id="r-1",
        session_id="s-1",
        user_id="c-1",
        started=0.0,
        duration_s=1.0,
        synthetic=False,
        captured=True,
        result="completed",
        rule_id="",
        reply_redacted=False,
        input="Where is AB-10010?",
        reply="Your order AB-10010 is on its way.",
        route="agent",
        termination="completed",
        cost_usd=0.0,
        model_calls=1,
        malformed=0,
        unbacked_promise=False,
        tools=(ToolUse("get_order", "read", "ok", {"id": "AB-10010"}, {"id": "AB-10010"}, 0.1),),
    )
    return Turn(**{**base, **changes})  # type: ignore[arg-type]


# (row, the turn, each online evaluator's verdict in YAML order)
ONLINE: list[tuple[str, Turn, list[str]]] = [
    ("a captured turn is judged on what was sent", _turn(), ["pass", "pass"]),
    (
        "an order nobody read, sent",
        _turn(reply="Your order AB-99999 is on its way."),
        ["fail", "pass"],
    ),
    (
        "an uncaptured turn has no response: skipped",
        _turn(captured=False, reply=None),
        ["skip", "skip"],
    ),
    ("a synthetic turn is the canary's", _turn(synthetic=True), []),
]


@pytest.mark.discharges("AAC-0014")
@pytest.mark.parametrize(("name", "turn", "verdicts"), ONLINE, ids=[o[0] for o in ONLINE])
def test_the_watch_judges_a_turn_at_the_online_position(
    name: str, turn: Turn, verdicts: list[str]
) -> None:
    results = online.judge(plan_of(YAML), [turn], agent="support-agent")
    assert [r.verdict for r in results] == verdicts
    assert [r.evaluator for r in results] == ["no_ungrounded_entity", "no_pii_echo"][
        : len(verdicts)
    ]
