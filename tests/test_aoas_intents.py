"""Every intent example the AOAS gives is read the way the AOAS says it is answered.

The AOAS's `examples` are "customer words that carry this intent and no other",
and its schema says a matcher is judged against them (T-094). So the cases are
read from the AOAS, never copied here: an example added there is a case here.

Two questions per example. Which intent did the router recognise — exactly the
one the AOAS names, or none for `other`. And where did it send the turn: a
handoff to its escalation rule, a refusal to a refusal, and a direct intent,
once it names one order, to the deterministic handler for that intent (an
example naming no order goes to the loop, P-DIRECT).

Found by a CCA-F case on 3 Oct: "where is my money back" was answered with the
parcel's status, and about fifteen of the AOAS's own examples were not
recognised at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from support_agent.contracts import Agentic, Direct, Escalate, Refuse, Route
from support_agent.router import route

ROOT = Path(__file__).resolve().parent.parent
WORLD = yaml.safe_load((ROOT / "worlds" / "clothing.yaml").read_text())
AOAS = yaml.safe_load((ROOT / "worlds" / WORLD["spec"]["path"]).resolve().read_text())

# The AOAS's intent names, in this agent's vocabulary. The two diverged before
# the AOAS named its intents; the mapping is the one place they meet.
OURS = {
    "order_status": "order_status",
    "refund_status": "refund_status",
    "cancel": "cancel_order",
    "return": "return_request",
    "address_change": "address_change",
    "refund_request": "refund_request",
    "exchange_request": "exchange_request",
    "damaged_item": "damaged_item",
    "other": None,
}
HANDOFF_RULE = {"human": "asked-for-human", "lost_in_transit": "lost-in-transit"}

# [AOAS intent, how it is answered, example]
EXAMPLES = [
    (name, intent["answered_by"], example)
    for name, intent in AOAS["intents"].items()
    for example in intent["examples"]
]


def recognised(decision: Route) -> set[str]:
    if isinstance(decision, Direct):
        return {decision.intent.value}
    if isinstance(decision, Agentic):
        return {i.value for i in decision.candidate_intents}
    return set()


@pytest.mark.discharges("P-DIRECT", "AHC-0094")
@pytest.mark.parametrize(
    ("intent", "answered_by", "example"), EXAMPLES, ids=[e[2] for e in EXAMPLES]
)
def test_every_aoas_example_is_read_as_its_intent(
    intent: str, answered_by: str, example: str
) -> None:
    decision = route(example)
    if answered_by == "handoff":
        assert isinstance(decision, Escalate) and decision.rule_id == HANDOFF_RULE[intent], decision
    elif answered_by == "refusal":
        assert isinstance(decision, Refuse), decision
    else:
        expected = {OURS[intent]} if OURS[intent] else set()
        assert recognised(decision) == expected, decision


DIRECT = [(n, e) for n, a, e in EXAMPLES if a == "direct"]


@pytest.mark.discharges("P-DIRECT", "P-REFUND-STATUS")
@pytest.mark.parametrize(("intent", "example"), DIRECT, ids=[d[1] for d in DIRECT])
def test_a_direct_example_naming_one_order_is_answered_by_its_handler(
    intent: str, example: str
) -> None:
    named = example if "AB-" in example else f"{example.rstrip('?.!')} for AB-10003?"
    decision = route(named)
    assert isinstance(decision, Direct) and decision.handler == OURS[intent], decision
