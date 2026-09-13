"""Which of this agent's modules would a different agent keep?

Three layers, and the middle one is the dangerous one:

**mechanism** — no agent's domain reaches it. A second agent keeps the file.
**parameterised** — universal code, this agent's values. A second agent keeps the
code and replaces the data, which is why it *looks* shared and behaves
differently per agent: a bug in the values reads as a bug in the mechanism.
**per-agent** — exists because this domain exists. A second agent writes its own.

Assigned by reading, not by grep: the measurement that counts entity words gives
a floor (3.4% of executable lines on 12 September 2026) and misses every
threshold, template and rule set that is this agent's and names no entity.

**A module with no layer fails the test**, so the classification cannot rot
quietly as modules are added.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "support_agent"

MECHANISM = {
    "__init__.py",
    "cassette/__init__.py",
    "conformance.py",
    "context/__init__.py",
    "contracts/__init__.py",
    "contracts/human.py",
    "contracts/ids.py",
    "contracts/model.py",
    "contracts/protocols.py",
    "contracts/results.py",
    "contracts/tools.py",
    "cost/__init__.py",
    "entrypoint/__init__.py",
    "entrypoint/handoff.py",
    "entrypoint/pending.py",
    "entrypoint/persist.py",
    "escalation/__init__.py",
    "escalation/capacity.py",
    "escalation/store.py",
    "escalation/workflow.py",
    "flow/__init__.py",
    "idempotency/__init__.py",
    "idempotency/postgres.py",
    "identity/__init__.py",
    "llm/__init__.py",
    "loop/__init__.py",
    "loop/dispatch.py",
    "loop/freshness.py",
    "loop/plan.py",
    "loop/screen.py",
    "resilience/__init__.py",
    "reviewer/__init__.py",
    "serve/__init__.py",
    "state/__init__.py",
    "state/postgres.py",
    "telemetry/__init__.py",
    "telemetry/names.py",
    "telemetry/redaction.py",
    "tools/__init__.py",
    "trigger/__init__.py",
    "approvals/store.py",
    "approvals/workflow.py",
}
"""Kept whole by a second agent. The escalation and approval *workflows* are here
and their *policies* are not: how an approval expires is universal, what needs
one is this shop's."""

PARAMETERISED = {
    "approvals/__init__.py",  # re-exports the universal workflow *and* this shop's refund tool
    "binding.py",  # the shape is universal; the scope names are this deployment's
    "approvals/policy.py",  # the threshold, the TTL, the states a refund is owed in
    "config/__init__.py",  # this deployment's models, budgets, endpoints
    "escalation/rules.py",  # the Tier 2 rule set and the facts it reads
    "entrypoint/promise.py",  # the gate is universal; the phrase table is English and this voice
    "policy/__init__.py",  # the engine is universal; the claim patterns are this domain's
    "router/__init__.py",  # the engine is universal; the intents and refusals are not
    "telemetry/contract.py",  # the span contract: mechanism, with this agent's span names in it
}
"""Code a second agent keeps and values it replaces. **The layer to watch:**
these read as shared and are not, so the values carry a version and the code
carries a test that the values are reached."""

PER_AGENT = {
    "approvals/refund.py",  # a refund tool, an order's total, a policy approver
    "contracts/domain.py",  # this domain's entities
    "entrypoint/direct.py",  # order status, refund status
    "escalation/wording.py",  # what this shop says to its customers
    "ui/__init__.py",  # a support chat page
}
"""Written afresh by a second agent, from its own specification."""

LAYERS = {"mechanism": MECHANISM, "parameterised": PARAMETERISED, "per-agent": PER_AGENT}

PREDICTION = """Written 2026-09-12, before a second agent exists.

Of 55 modules: 43 mechanism, 7 parameterised, 5 per-agent. By executable lines
the mechanism share is larger still, because the per-agent modules are small.

**The prediction, for G2.6 to measure.** A second agent in another domain, built
from the same universal specs, will keep **roughly four fifths of this code
unchanged**, replace the six parameterised files' *values* while keeping their
code, and write its own five. The number worth arguing about is not the four
fifths — it is how much of the parameterised layer survives contact with a
domain whose shape differs: a booking has dates compared with dates, and
`router` and `escalation/rules` are where that will show first.

**What would falsify it.** A mechanism module that a hotel agent has to edit was
never mechanism — it was parameterised and nobody noticed, because one agent
cannot tell the difference.

**Found while classifying, and worth fixing before a second agent arrives.**
`approvals/__init__.py` was read as mechanism and the measurement disagreed: it
re-exports `refund_tool` and its constants beside the universal workflow, so a
second agent must edit a file in a package that is otherwise entirely reusable.
The seam is in the wrong place — a domain tool built *on* the approval workflow
belongs beside the domain, not inside the mechanism that serves it. The audit
found it by making the reading and the measurement contradict each other, which
is the argument for doing both."""


def classify() -> dict[str, str]:
    return {module: layer for layer, members in LAYERS.items() for module in members}


def modules() -> set[str]:
    return {str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "__pycache__" not in str(p)}


__all__ = ["LAYERS", "PREDICTION", "classify", "modules"]
