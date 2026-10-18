"""The record's turns as `evalharness.Session`s: what the domain patterns read."""

from __future__ import annotations

from typing import Any

import pytest

from agent_harness.watch.record import ToolUse, Turn
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
        result="completed",
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
