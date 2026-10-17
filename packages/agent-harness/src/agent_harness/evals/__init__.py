"""Checks as plug-ins: one request in, one result out, every check placed by YAML.

L7 and L12 (the architecture deck, slides 93–94). Every evaluator — our reply
rules, a guardrail's logged verdict, Presidio, an Azure built-in, an open judge
model — sits behind one port, `Evaluator`, and is handed one shape,
`EvalRequest`, and answers in one shape, `EvalResult`. A provider's own shape
stays inside its adapter: our rules never see Azure's data mapping and Azure
never sees a `policy.Context`.

Where each one runs is configuration (`evaluators.yaml`, read by `evals.plan`):

    reply     inline, before the customer reads it; a fail blocks (safe reply)
    online    after the turn, sampled; a fail is a finding or an alert
    release   over golden cases and scenarios; a fail blocks the release

**Each evaluator declares what it needs.** A request that lacks it is
`skip: missing <input>`, never a failure, so a turn with no retrieval is not
failed by a groundedness check. And a position that can never provide it
refuses the evaluator at startup, so a tool-selection check cannot be placed
where there are no tool definitions.

No agent and no vendor SDK is imported here or in any module beneath it (an
import contract holds that line); a vendor's evaluators are its own adapter
module, registered by name.
"""

from __future__ import annotations

# The shapes and the port live in `evalharness` (eval-harness-framework, D2,
# 17 Oct 2026), so a check written for this harness runs anywhere that package
# does. Every name below is re-exported unchanged; what this package keeps is
# the harness's own placing of checks (`plan`, `screens`, `rule`).
from evalharness import (
    NEEDS,
    EvalRequest,
    EvalResult,
    Evaluator,
    Expected,
    Finding,
    Meta,
    Outcome,
    Passage,
    Response,
    Scale,
    Speed,
    judge,
    skipped,
)

__all__ = [
    "NEEDS",
    "EvalRequest",
    "EvalResult",
    "Evaluator",
    "Expected",
    "Finding",
    "Meta",
    "Outcome",
    "Passage",
    "Response",
    "Scale",
    "Speed",
    "judge",
    "skipped",
]
