"""Generate the frozen golden set from the state machine.

C3. The eligibility matrix in the functional spec is a *generator*, not a list:
actions × order states, plus the boundary cases that carry the interesting
failures. Writing them by hand would mean writing the ones anybody thought of.

Pairwise reduction (`allpairspy`) collapses the third and fourth dimensions —
days since delivery, final-sale flag — because most defects are found by a pair
of parameters interacting, and covering every triple costs an order of magnitude
more cases for very little more coverage. The boundary rows are then added back
by hand, because a boundary is exactly what a sampling strategy will miss:
day 30 and day 31 differ by one and by everything.

    uv run python evals/generate_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from allpairspy import AllPairs

from support_agent.contracts import OrderStatus

OUT = Path(__file__).parent / "golden" / "eligibility.jsonl"

ACTIONS = ["cancel_order", "open_return_request", "change_address"]
STATES = [s.value for s in OrderStatus]
DAYS = [0, 30, 31]
FINAL_SALE = [False, True]

BOUNDARIES = [
    ("open_return_request", "delivered", 30, False, True, "the last day of the window"),
    ("open_return_request", "delivered", 31, False, False, "one day past the window"),
    ("open_return_request", "delivered", 0, True, False, "final sale, inside the window"),
    ("cancel_order", "confirmed", 0, False, True, "the last state that can be cancelled"),
    ("cancel_order", "picked", 0, False, False, "the first state that cannot"),
    ("change_address", "pending", 0, False, True, "the only state the address is ours"),
    ("change_address", "confirmed", 0, False, False, "one state too late"),
]


def expected(action: str, status: str, days: int, final_sale: bool) -> bool:
    """What the *server* should decide. Deliberately a second implementation.

    If this agreed with `evals/world.py` by sharing code it would agree with any
    bug in it too. Two independent statements of the same rule disagree loudly
    when one is wrong, which is the entire point of an expectation.
    """
    if action == "cancel_order":
        return status in {"pending", "confirmed"}
    if action == "change_address":
        return status == "pending"
    if action == "open_return_request":
        return status == "delivered" and days <= 30 and not final_sale
    raise AssertionError(f"unmodelled action {action}")


def generate() -> list[dict]:
    cases: list[dict] = []
    seen: set[tuple] = set()

    for boundary in BOUNDARIES:
        action, status, days, final_sale, allowed, why = boundary
        assert expected(action, status, days, final_sale) is allowed, (
            f"boundary case disagrees with the expectation function: {boundary}"
        )
        key = (action, status, days, final_sale)
        seen.add(key)
        cases.append(
            {
                "id": f"golden-{len(cases):03d}",
                "action": action,
                "status": status,
                "days_since_delivery": days,
                "final_sale": final_sale,
                "expected_allowed": allowed,
                "boundary": why,
            }
        )

    for action, status, days, final_sale in AllPairs([ACTIONS, STATES, DAYS, FINAL_SALE]):
        key = (action, status, days, final_sale)
        if key in seen:
            continue
        seen.add(key)
        cases.append(
            {
                "id": f"golden-{len(cases):03d}",
                "action": action,
                "status": status,
                "days_since_delivery": days,
                "final_sale": final_sale,
                "expected_allowed": expected(action, status, days, final_sale),
                "boundary": None,
            }
        )

    return cases


def main() -> None:
    cases = generate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(c) + "\n" for c in cases))
    allowed = sum(1 for c in cases if c["expected_allowed"])
    print(f"{len(cases)} cases -> {OUT}")
    print(f"  {allowed} expect allow, {len(cases) - allowed} expect refuse")
    print(f"  {sum(1 for c in cases if c['boundary'])} boundary cases")
    print(f"  full grid would be {len(ACTIONS) * len(STATES) * len(DAYS) * len(FINAL_SALE)}")


if __name__ == "__main__":
    main()
