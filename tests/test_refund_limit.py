"""The automatic refund limit is the AOAS's, wherever else it is written (F-088).

Generation run 4 (NOTES M11) found it stated twice — the AOAS bounds
`issue_refund.authority.agent_when` at a total, the profile's thresholds carry
`refund_without_a_person_inr` — and nothing saying which governs if they part.
It is in fact three times: the approval policy's default is the one enforced.
The AOAS governs; every other copy must equal it, and the build fails if not.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from support_agent.approvals.policy import Policy

ROOT = Path(__file__).resolve().parent.parent
WORLD = yaml.safe_load((ROOT / "worlds" / "clothing.yaml").read_text())
AOAS = yaml.safe_load((ROOT / "worlds" / WORLD["spec"]["path"]).resolve().read_text())
PROFILE = yaml.safe_load((ROOT / "harness-profile.yaml").read_text())


def aoas_limit() -> Decimal:
    bounds = [
        c["at_most"]
        for c in AOAS["operations"]["issue_refund"]["authority"]["agent_when"]
        if c.get("field") == "total" and "at_most" in c
    ]
    assert len(bounds) == 1, "the AOAS states one limit on the total"
    return Decimal(bounds[0])


# [where the limit is written again, its value there]
COPIES = [
    ("the profile's threshold", Decimal(PROFILE["thresholds"]["refund_without_a_person_inr"])),
    ("the approval policy's default, which is enforced", Policy().refund_threshold),
]


@pytest.mark.discharges("P-REFUND", "Q-AUTO-LIMIT")
@pytest.mark.parametrize(("where", "value"), COPIES, ids=[c[0] for c in COPIES])
def test_every_copy_of_the_refund_limit_is_the_aoas(where: str, value: Decimal) -> None:
    assert value == aoas_limit(), (
        f"{where} says {value}; the AOAS, which governs, says {aoas_limit()}"
    )
