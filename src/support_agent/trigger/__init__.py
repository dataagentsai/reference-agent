"""What starts a turn, and whether it should have.

L15 · L10 · P1. AAC-0076 — *triggers fire once and only once*, and **AHC-0053**,
*a trigger produces exactly one run*.

The layers were wrong here until 2026-09-05: this module claimed L14, which is
human-in-the-loop, and a trigger is plainly not a person. The catalogue already
had the right home — L15 because a trigger is configuration, L10 because
duplicate firing is a failure mode — and nothing checked our claim against it.
A mislabelled layer is not cosmetic: it is how a capability ends up counted in
the wrong row of a coverage report.

The obligation is about the edge, not the agent: **an agent that runs twice takes
every action twice**, and it does so correctly each time. Nothing inside the run
is wrong, which is why nothing inside the run can catch it.

## Why idempotency does not already cover this

The ledger keys on `run + step + iteration`. A duplicate delivery produces a
whole second run with its own run id, so the two never share a key space and the
ledger cannot see across them. R-006 found the same thing from the inside — a
customer who asks three times is three runs — and this is the outside half of
it. **The defence sat one layer below where the duplicate happens.**

## Three failures, and this module answers two

*Fires twice.* A webhook redelivered because the acknowledgement was slow, a
customer clicking Send twice, a queue with at-least-once semantics — which is
every queue worth using. Answered here: a settled delivery is refused.

*Fires again mid-run.* Two turns for one conversation in flight at once, each
reading state before the other has written. Answered here: an in-flight delivery
is refused, and told apart from a duplicate because the operator response
differs — one is a retry to swallow, the other is a race to investigate.

*Never fires.* Not answerable here, and pretending otherwise would be the worst
option: **you cannot detect an absence from inside the thing that is absent.**
It needs a clock the monitoring side owns (AHC-0055), and in a test it is
`agenttwin.omission` — a turn that never arrives means a required effect never
happens, and the omission oracle sees exactly that. One idea, two positions.

## Claim, then settle

A delivery is claimed before the run and settled after it. The gap between the
two is what makes an overlapping run visible at all; a design that only recorded
completed deliveries would let two concurrent copies both pass the check.

Settling happens even when the run fails. A delivery that was tried and failed
has still been delivered, and re-running it on redelivery would repeat whatever
side effects it managed before failing.
"""

from support_agent.trigger.durable import (
    CLAIM_TTL_S,
    WORKFLOWS,
    DeliveryClaim,
    TemporalDeliveries,
    worker_for,
)
from support_agent.trigger.log import (
    Delivery,
    DeliveryLog,
    DuplicateDelivery,
    InMemoryDeliveryLog,
    OverlappingRun,
    State,
    TriggerRefused,
    once,
)

__all__ = [
    "CLAIM_TTL_S",
    "WORKFLOWS",
    "Delivery",
    "DeliveryClaim",
    "DeliveryLog",
    "DuplicateDelivery",
    "InMemoryDeliveryLog",
    "OverlappingRun",
    "State",
    "TemporalDeliveries",
    "TriggerRefused",
    "once",
    "worker_for",
]
