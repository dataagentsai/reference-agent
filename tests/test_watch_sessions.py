"""The record's turns as `evalharness.Session`s: what the domain patterns read."""

from __future__ import annotations

from typing import Any

import pytest

from agent_harness import telemetry as tel
from agent_harness.watch.record import Node, ToolUse, Turn, Work, background
from agent_harness.watch.sessions import session_of, sessions


def turn(
    started: float,
    *,
    session: str = "cnv_1",
    said: str | None = "hi",
    reply: str | None = "hello",
    tools: tuple[ToolUse, ...] = (),
    ended: str = "goal_reached",
    synthetic: bool = False,
    result: str = "completed",
) -> Turn:
    return Turn(
        trace_id=f"t{started}",
        run_id=f"r{started}",
        session_id=session,
        user_id="PH-1001",
        started=started,
        duration_s=1.0,
        synthetic=synthetic,
        captured=True,
        result=result,
        rule_id="",
        reply_redacted=False,
        input=said,
        reply=reply,
        route="model",
        termination=ended,
        cost_usd=0.0,
        model_calls=1,
        malformed=0,
        unbacked_promise=False,
        tools=tools,
    )


def use(name: str, outcome: str = "ok", result: Any = None, **arguments: Any) -> ToolUse:
    return ToolUse(
        name=name,
        side_effect="read",
        outcome=outcome,
        arguments=arguments,
        result=result,
        duration_s=0.1,
    )


def kinds(t: Turn) -> list[tuple[str, str, bool]]:
    return [(s.kind, s.name, s.is_error) for s in session_of("s", [t]).steps]


SHAPES = [
    (
        "a lookup",
        turn(1, tools=(use("get_claim", id="CLM-1"),)),
        [
            ("user", "", False),
            ("tool_call", "get_claim", False),
            ("tool_result", "get_claim", False),
            ("assistant", "", False),
        ],
    ),
    (
        "an error is not done",
        turn(1, tools=(use("get_claim", "error"),)),
        [
            ("user", "", False),
            ("tool_call", "get_claim", False),
            ("tool_result", "get_claim", True),
            ("assistant", "", False),
        ],
    ),
    (
        "a refusal is not done",
        turn(1, tools=(use("issue_payout", "refused"),)),
        [
            ("user", "", False),
            ("tool_call", "issue_payout", False),
            ("tool_result", "issue_payout", True),
            ("assistant", "", False),
        ],
    ),
    (
        "a replay is done",
        turn(1, tools=(use("issue_payout", "replayed"),)),
        [
            ("user", "", False),
            ("tool_call", "issue_payout", False),
            ("tool_result", "issue_payout", False),
            ("assistant", "", False),
        ],
    ),
    (
        "an ending that is a decision",
        turn(1, ended="awaiting_approval"),
        [("user", "", False), ("assistant", "", False), ("decision", "awaiting_approval", False)],
    ),
    (
        "a hand-over the router made by rule",
        turn(1, ended="", result="escalated"),
        [("user", "", False), ("assistant", "", False), ("decision", "awaiting_human", False)],
    ),
    (
        "an ending that is not",
        turn(1, ended="goal_reached"),
        [("user", "", False), ("assistant", "", False)],
    ),
    ("words not captured", turn(1, said=None, reply=None), []),
]


@pytest.mark.parametrize(("name", "t", "steps"), SHAPES, ids=[s[0] for s in SHAPES])
def test_a_turn_becomes_steps_in_order(
    name: str, t: Turn, steps: list[tuple[str, str, bool]]
) -> None:
    assert kinds(t) == steps


def test_a_call_and_its_result_share_an_id() -> None:
    call, result = session_of("s", [turn(1, tools=(use("get_claim"),))]).steps[1:3]
    assert call.call_id == result.call_id == "r1:0"


def test_conversations_are_grouped_ordered_and_the_canary_left_out() -> None:
    found = sessions(
        [
            turn(5, session="b"),
            turn(2, session="a", said="second"),
            turn(1, session="a", said="first", ended="awaiting_human"),
            turn(3, session="c", synthetic=True),
        ]
    )
    assert [s.id for s in found] == ["a", "b"]
    first = found[0]
    assert [s.text for s in first.steps if s.kind == "user"] == ["first", "second"]
    assert first.outcome == "goal_reached"  # how the last turn ended
    assert ("decision", "awaiting_human") in [(s.kind, s.name) for s in first.steps]


# ------------------------------------------------------------------ work outside a turn
def work(started: float, *tools: ToolUse, session: str = "cnv_1") -> Work:
    return Work(trace_id=f"w{started}", session_id=session, started=started, tools=tools)


PLACED = [
    (
        "a payout carried out inside the turn goes before its reply",
        [turn(10, tools=(use("request_payout"),), reply="It has been issued.")],
        [work(10.5, use("issue_payout"))],
        [
            "user",
            "tool_call:request_payout",
            "tool_result:request_payout",
            "tool_call:issue_payout",
            "tool_result:issue_payout",
            "assistant",
        ],
    ),
    (
        "a payout a person approved later goes between the turns",
        [
            turn(10, reply="Sent to a handler.", ended="awaiting_approval"),
            turn(100, said="Paid yet?", reply="Yes."),
        ],
        [work(50, use("issue_payout"))],
        [
            "user",
            "assistant",
            "decision:awaiting_approval",
            "tool_call:issue_payout",
            "tool_result:issue_payout",
            "user",
            "assistant",
        ],
    ),
]


@pytest.mark.parametrize(("name", "turns", "done", "steps"), PLACED, ids=[p[0] for p in PLACED])
def test_work_for_a_conversation_goes_where_it_happened(
    name: str, turns: list[Turn], done: list[Work], steps: list[str]
) -> None:
    found = session_of("cnv_1", turns, done).steps
    assert [f"{s.kind}:{s.name}" if s.name else s.kind for s in found] == steps


def test_work_for_another_conversation_stays_out() -> None:
    (found,) = sessions([turn(1)], [work(1.5, use("issue_payout"), session="cnv_other")])
    assert "tool_call" not in [s.kind for s in found.steps]


def test_background_reads_tool_calls_that_name_a_conversation_from_traces_without_a_turn() -> None:
    def node(trace: str, name: str, at: float, **attributes: Any) -> Node:
        return Node(trace_id=trace, name=name, attributes=attributes, start=at, end=at + 0.1)

    nodes = [
        node("t1", "agent.turn", 1.0),
        node(
            "t1", "agent.tool", 1.2, **{tel.GEN_AI_TOOL_NAME: "get_claim", tel.SESSION_ID: "cnv_1"}
        ),
        node("t2", "agent.approval.carry_out", 5.0, **{tel.SESSION_ID: "cnv_1"}),
        node(
            "t2",
            "agent.tool",
            5.1,
            **{
                tel.GEN_AI_TOOL_NAME: "issue_payout",
                tel.SESSION_ID: "cnv_1",
                tel.TOOL_OUTCOME: "ok",
                tel.TOOL_RESULT: '{"status": "paid"}',
            },
        ),
        node(
            "t3", "agent.tool", 9.0, **{tel.GEN_AI_TOOL_NAME: "get_claim"}
        ),  # names no conversation
    ]
    (found,) = background(nodes)
    assert (found.trace_id, found.session_id, found.started) == ("t2", "cnv_1", 5.1)
    assert [(u.name, u.outcome, u.result) for u in found.tools] == [
        ("issue_payout", "ok", {"status": "paid"})
    ]


def test_work_scope_names_the_conversation_on_every_span_inside_it_and_only_there() -> None:
    exporter = tel.configure(capture_payloads=True)
    with tel.span("before"):
        pass
    with (
        tel.work_scope("cnv_9", "apr_1") as keep,
        tel.span("agent.approval.carry_out"),
        tel.span("agent.tool"),
    ):
        assert tel.capturing()
    with tel.span("after"):
        pass
    sessions_of = {
        s.name: (s.attributes or {}).get(tel.SESSION_ID) for s in exporter.get_finished_spans()
    }
    assert keep is True
    assert sessions_of == {
        "before": None,
        "agent.approval.carry_out": "cnv_9",
        "agent.tool": "cnv_9",
        "after": None,
    }
    assert not tel.capturing()
    tel.configure()


# ------------------------------------------------------- from any trace source
def _span(name: str, at: float, **attributes: str) -> Any:
    from evalharness.traces import Span

    return Span(
        trace_id="tr1", span_id=f"s{at}", name=name, start=at, end=at + 0.1, attributes=attributes
    )


@pytest.mark.parametrize(
    ("spans", "expected"),
    [
        (  # as App Insights keeps them: every value a string, no content captured
            [
                _span(
                    "agent.turn",
                    1.0,
                    **{
                        "agent.captured": "False",
                        "agent.turn.result": "escalated",
                        "agent.turn.rule_id": "asked-for-human",
                        "agent.run.id": "run_1",
                        "user.id": "PH-1001",
                        "agent.synthetic": "False",
                    },
                ),
                _span(
                    "agent.tool",
                    1.2,
                    **{
                        "gen_ai.tool.name": "get_policy",
                        "agent.tool.outcome": "ok",
                        "agent.tool.side_effect": "read",
                    },
                ),
            ],
            ("escalated", "asked-for-human", False, None, ("get_policy",)),
        ),
    ],
)
def test_turns_rebuild_from_a_trace_source(spans: list[Any], expected: tuple[Any, ...]) -> None:
    from agent_harness.watch.record import from_trace_spans, turns

    (t,) = turns(from_trace_spans(spans))
    assert (t.result, t.rule_id, t.captured, t.input, tuple(u.name for u in t.tools)) == expected


@pytest.mark.parametrize(
    ("captured", "tool", "unrecorded"),
    [
        (
            True,
            ToolUse("get_claim", "read", "ok", {"id": "C1"}, {"status": "approved"}, 0.1),
            set(),
        ),
        (
            False,
            ToolUse("get_claim", "read", "ok", {"id": "C1"}, {"status": "approved"}, 0.1),
            {"text"},
        ),
        (True, ToolUse("get_claim", "read", "ok", None, None, 0.1), {"arguments", "results"}),
        (
            True,
            ToolUse("get_claim", "read", "error", {"id": "C1"}, None, 0.1),
            set(),
        ),  # failed: no result due
    ],
)
def test_a_session_says_what_its_record_does_not_hold(
    captured: bool, tool: ToolUse, unrecorded: set[str]
) -> None:
    from dataclasses import replace

    t = replace(turn(1.0, tools=(tool,)), captured=captured)
    assert set(session_of("cnv_1", [t]).unrecorded) == unrecorded
