"""The refusal list, and the line between refusing and over-refusing.

The AOAS declares six things this agent refuses. Three were enforced; a delivery
date is refused where one could be invented, by the reply guardrail, because no
phrasing of the question is the problem. Style advice and fraud adjudication were
declared and nothing enforced them.

The negatives are half the point. A refusal rule that matches the *noun* refuses
the customers it exists to serve — "the fit was wrong, I want to return it" is a
return — and the escalate rule two files over carries the scar of learning it.
"""

from __future__ import annotations

import pytest

from support_agent import router

# (name, what the customer says, the rule that must refuse it — None to be served)
CASES = [
    ("a discount is asked for", "can I get a discount?", "R-DISCOUNT"),
    ("another customer's order", "what about my friend's order?", "R-OTHER-CUSTOMER"),
    ("an account change", "delete my account please", "R-ACCOUNT"),
    ("which size to buy", "what size should I order in this shirt?", "R-STYLE"),
    ("whether it will fit", "will this fit me?", "R-STYLE"),
    ("sizing up or down", "should I size up?", "R-STYLE"),
    ("asked outright for style advice", "I need some style advice", "R-STYLE"),
    ("is this fraud", "is this transaction fraud?", "R-FRAUD"),
    ("asked to confirm fraud", "can you confirm this was fraud on my card?", "R-FRAUD"),
    ("was it stolen", "was that parcel stolen?", "R-FRAUD"),
    # Served, not refused. Each contains a word a careless rule would match.
    ("a return because the fit was wrong", "the fit was wrong, I want to return AB-10003", None),
    ("a size that arrived wrong", "you sent the wrong size for AB-10003", None),
    ("a parcel that never came", "my order says delivered but it never arrived", None),
    ("a double charge", "I was charged twice for AB-10003", None),
    ("a plain status question", "where is my order AB-10003", None),
]


@pytest.mark.discharges("R-DISCOUNT", "R-OTHER-CUSTOMER", "R-ACCOUNT", "R-STYLE", "R-FRAUD")
@pytest.mark.parametrize(("name", "text", "rule"), CASES, ids=[c[0] for c in CASES])
def test_what_is_refused_and_what_is_served(name: str, text: str, rule: str | None) -> None:
    decision = router.route(text)
    if rule is None:
        assert decision.kind != "refuse", f"refused a customer it should serve: {decision}"
        return
    assert decision.kind == "refuse", f"served something the spec refuses: {decision}"
    assert decision.rule_id == rule, "the refusal names the statement it enforces"


@pytest.mark.discharges("R-STYLE", "R-FRAUD")
def test_every_declared_refusal_is_enforced_somewhere() -> None:
    """The ratchet. A rule added to the spec's list and to nothing else is a
    statement that reads as enforced and refuses nobody."""
    declared = {"R-DISCOUNT", "R-OTHER-CUSTOMER", "R-ACCOUNT", "R-STYLE", "R-FRAUD"}
    routed = {rule_id for rule_id, _, _ in router.Rules().refuse}
    assert declared - routed == set(), "declared in the AOAS, enforced by nothing"


# (name, what the customer says, the route it must take, the intent if direct)
REFUND_UTTERANCES = [
    ("asking for money back is a request", "please refund my order AB-10003", "agentic", None),
    ("so is wanting money back", "I want my money back for AB-10003", "agentic", None),
    ("asking where it is is a status question",
     "where is my refund for AB-10003", "direct", "refund_status"),
    ("so is asking whether it happened",
     "has my refund been processed for AB-10003", "direct", "refund_status"),
    ("and asking for the status outright",
     "refund status for AB-10003", "direct", "refund_status"),
    ("an order status question is still one",
     "where is my order AB-10003", "direct", "order_status"),
]  # fmt: skip


@pytest.mark.discharges("P-REFUND-STATUS", "P-REFUND", "P-DIRECT", "P-DIRECT-READS")
@pytest.mark.parametrize(
    ("name", "text", "route", "intent"), REFUND_UTTERANCES, ids=[u[0] for u in REFUND_UTTERANCES]
)
def test_asking_for_a_refund_is_not_asking_after_one(
    name: str, text: str, route: str, intent: str | None
) -> None:
    """F-030, found by the first live run against a real model.

    `\\brefund\\b` matched *"please refund my order"*, so a request for money back
    was answered with its status — true, useless, and not what was asked. The
    same pattern made *"where is my refund"* match two intents and go to the
    loop, so the one utterance the deterministic route exists for was the one it
    missed. It answered precisely the wrong set.

    Third time this shape has appeared here: the escalate rule matched the noun
    *agent*, and R-STYLE would have matched *fit*. **Anchor on the asking.**
    """
    decision = router.route(text)
    assert decision.kind == route, f"{text!r} took the {decision.kind} route"
    if intent is not None:
        assert decision.intent.value == intent
