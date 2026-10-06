"""What the far end's answer to a refund means, and what a retry is handed.

T-095, from a CCA-F case: a provider timing out in bursts and a closed card came
back as the same error, so the agent retried the card and told the customer to
try tomorrow. Here each answer is read by its `kind` (AHC-0043). Found on the
way: every `allowed: false` counted as success (F-089), and the tool client
recorded a timeout as the call's answer and replayed it to every retry.
"""

from __future__ import annotations

import pytest

from support_agent import identity as ident
from support_agent.approvals.refund import _outcome_of
from support_agent.contracts import (
    IdempotencyKey,
    Identity,
    RunId,
    SideEffectClass,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
)
from support_agent.requests import InMemoryRequests


def answer(**structured: object) -> ToolResult:
    return ToolResult(name="issue_refund", structured=structured)


# [name, what the far end answered, carried out?, declined?] — None: retried
OUTCOMES = [
    ("refunded", answer(allowed=True, reason="allowed", id="AB-1"), True, False),
    (
        "declined for good",
        answer(allowed=False, reason="card closed", kind="declined"),
        False,
        True,
    ),
    (
        "refused on a precondition (F-089: was counted done)",
        answer(allowed=False, reason="already refunded"),
        False,
        False,
    ),
    (
        "a timeout the far end calls transient",
        answer(allowed=False, reason="timeout", kind="transient"),
        None,
        None,
    ),
    (
        "a protocol error",
        ToolResult(name="issue_refund", text="gone", is_error=True, error_channel="protocol"),
        None,
        None,
    ),
    (
        "an execution error",
        ToolResult(name="issue_refund", text="bad", is_error=True, error_channel="execution"),
        False,
        False,
    ),
]


@pytest.mark.discharges("AHC-0043", "P-REFUND-DECLINED", "op:issue_refund")
@pytest.mark.parametrize(("name", "said", "ok", "declined"), OUTCOMES, ids=[o[0] for o in OUTCOMES])
def test_a_refund_answer_is_read_by_its_kind(
    name: str, said: ToolResult, ok: bool | None, declined: bool | None
) -> None:
    if ok is None:
        with pytest.raises(ToolUnavailable):
            _outcome_of(said)
        return
    outcome = _outcome_of(said)
    assert (outcome.ok, outcome.declined) == (ok, declined)


class Sequence:
    """A transport answering a write with each result in turn."""

    def __init__(self, *results: ToolResult) -> None:
        self.results = list(results)
        self.calls = 0

    async def advertised(self) -> tuple[tuple[ToolSpec, ...], tuple[str, ...]]:
        spec = ToolSpec(
            name="issue_refund",
            description="a refund",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=SideEffectClass.IRREVERSIBLE,
        )
        return (spec,), ()

    async def invoke(self, name, arguments, *, caller, idempotency_key=None) -> ToolResult:
        self.calls += 1
        return self.results.pop(0)


TIMEOUT = answer(allowed=False, reason="timeout", kind="transient")
DONE = answer(allowed=True, reason="allowed", id="AB-1")
CLOSED = answer(allowed=False, reason="card closed", kind="declined")

# [name, the far end's answers in turn, what the retry is handed, calls made]
RETRIES = [
    ("a timeout is not the call's answer: the retry reaches the far end", [TIMEOUT, DONE], DONE, 2),
    ("a refund that landed is replayed, not repeated", [DONE, DONE], DONE, 1),
    ("a decline is final: the retry is handed it again", [CLOSED, DONE], CLOSED, 1),
]


@pytest.mark.discharges("AHC-0043", "AHC-0053", "AAC-0047")
@pytest.mark.parametrize(
    ("name", "answers", "handed", "calls"), RETRIES, ids=[r[0] for r in RETRIES]
)
async def test_what_a_retry_under_the_same_key_is_handed(
    name: str, answers: list[ToolResult], handed: ToolResult, calls: int
) -> None:
    from support_agent.tools import GatedTools

    far = Sequence(*answers)
    tools = GatedTools(far, requests=InMemoryRequests())
    who = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
    key = IdempotencyKey(run_id=RunId("run_r"), step=0, iteration=0)

    await tools.call("issue_refund", {}, who, key)
    second = await tools.call("issue_refund", {}, who, key)

    assert second.structured == handed.structured
    assert far.calls == calls
