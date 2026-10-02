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
    "contracts/requests.py",
    "contracts/failures.py",
    "contracts/results.py",
    "contracts/tools.py",
    "cost/__init__.py",
    "entrypoint/__init__.py",
    "entrypoint/handoff.py",
    "entrypoint/persist.py",
    "entrypoint/ending.py",
    "escalation/__init__.py",
    "escalation/capacity.py",
    "escalation/durable.py",
    "escalation/desk.py",
    "escalation/workflow.py",
    "erasure/__init__.py",
    "erasure/retention.py",
    "flow/__init__.py",
    "requests/__init__.py",
    "requests/postgres.py",
    "identity/__init__.py",
    "identity/sessions.py",
    "llm/__init__.py",
    "loop/__init__.py",
    "loop/dispatch.py",
    "loop/freshness.py",
    "loop/plan.py",
    "loop/spend.py",
    "loop/screen.py",
    "loop/ends.py",
    "policy/reading.py",  # folds look-alike characters; no domain in it
    "resilience/__init__.py",
    "reviewer/__init__.py",
    "reviewer/guard.py",
    "reviewer/approvals.py",
    "channel/__init__.py",
    "portal/__init__.py",
    "serve/__init__.py",
    "serve/feedback.py",
    "state/__init__.py",
    "state/facts.py",
    "state/file.py",
    "state/postgres.py",
    "telemetry/__init__.py",
    "telemetry/counters.py",
    "telemetry/meters.py",
    "telemetry/names.py",
    "telemetry/redaction.py",
    "tools/__init__.py",
    "tools/mcp.py",
    "approvals/workflow.py",
    "approvals/durable.py",
    "approvals/notify.py",
    "approvals/desk.py",
    "watch/__init__.py",
    "watch/record.py",
    "watch/outcomes.py",
    "watch/langfuse.py",
}
"""Kept whole by a second agent. The escalation and approval *workflows* are here
and their *policies* are not: how an approval expires is universal, what needs
one is this shop's. `approvals/durable.py` is the same division inside Temporal:
the workflow waits for any action, and `approvals/refund.py` says which."""

PARAMETERISED = {
    "entrypoint/pending.py",  # universal approval flow; the refund replies are this shop's
    "approvals/__init__.py",  # re-exports the universal workflow *and* this shop's refund tool
    "binding.py",  # the shape is universal; the scope names are this deployment's
    "approvals/policy.py",  # the threshold, the TTL, the states a refund is owed in
    "config/__init__.py",  # this deployment's models, budgets, endpoints
    "escalation/rules.py",  # the Tier 2 rule set and the facts it reads
    "entrypoint/promise.py",  # the gate is universal; the phrase table is English and this voice
    "policy/__init__.py",  # the engine is universal; the claim patterns are this domain's
    "router/__init__.py",  # the engine is universal; the intents and refusals are not
    "telemetry/contract.py",  # the span contract: mechanism, with this agent's span names in it
    "watch/rules.py",  # the engine is universal; statuses, claims, write tools are this shop's
    "watch/checks.py",  # the checks are universal; the vocabulary they read is this shop's
    "watch/canary.py",  # the probe is universal; its cases ask this shop's questions (F-062)
    "watch/evidence.py",  # the verdict is universal; which rule evidences which AAC id is ours
    "contracts/reading.py",  # the fold is universal; the order-id shape is this store's
}
"""Code a second agent keeps and values it replaces. **The layer to watch:**
these read as shared and are not, so the values carry a version and the code
carries a test that the values are reached."""

PER_AGENT = {
    "approvals/refund.py",  # a refund tool, an order's total, a policy approver
    "contracts/domain.py",  # this domain's entities
    "entrypoint/direct.py",  # order status, refund status
    "entrypoint/opening.py",  # a customer's orders and this shop's queues, on opening
    "entrypoint/consent.py",  # which of this shop's actions a customer's words ask for
    "escalation/wording.py",  # what this shop says to its customers
    "ui/__init__.py",  # a support chat page
    "reviewer/page.py",  # a desk page, in this shop's words
}
"""Written afresh by a second agent, from its own specification."""

LAYERS = {"mechanism": MECHANISM, "parameterised": PARAMETERISED, "per-agent": PER_AGENT}

PREDICTION = """Written 2026-09-12, before a second agent exists.

Of 59 modules: 47 mechanism, 7 parameterised, 5 per-agent. By executable lines
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


__all__ = ["LAYERS", "PREDICTION", "SEAMS", "classify", "modules", "seams"]


# seam -> (where the variation lives, the pattern holding it, what a second
# agent changes). G0.10's second half: the classification above says *how much*
# is reusable, and this says *through what*. A seam with no pattern is a seam
# where a second agent edits shared code, which is the thing the classification
# cannot see — `router/__init__.py` and `escalation/rules.py` both read as
# parameterised, and only one of them has somewhere for the parameters to go.
SEAMS = {
    "which requests skip the model": (
        "router.Rules",
        "versioned rule set",
        "its own intents, refusals and escalation triggers, as data with a version",
    ),
    "what the deterministic path answers": (
        "entrypoint/direct.HANDLERS",
        "registry keyed by name",
        "its own handlers; the router names one and the registry resolves it",
    ),
    "when a person must decide": (
        "approvals.Policy",
        "versioned rule set",
        "the threshold, the validity window, and which states owe a refund",
    ),
    "when the conversation has earned a person": (
        "escalation.rules.RuleSet",
        "versioned rule set",
        "its own conditions over the same declared facts",
    ),
    "what the customer is told about the desk": (
        "escalation/wording.py",
        "template rendering",
        "every string, in its own voice",
    ),
    "what a claim is checked against": (
        "policy.CLAIM_PATTERNS",
        "versioned rule set",
        "the claims its domain makes, and what in a tool result supports them",
    ),
    "which tools exist and what they do": (
        "the world, projected",
        "projection from the specification",
        "nothing — a second world is a second YAML file and no code at all",
    ),
    "what authority an operation needs": (
        "binding.SCOPES",
        "map in the composition root",
        "the scope names its credential issuer uses",
    ),
    "how long a read stays usable": (
        "binding.FRESH_FOR_S",
        "value in the composition root",
        "the window its own concurrent writers make necessary",
    ),
    "where state is kept": (
        "CheckpointStore, Approvals, Escalations",
        "protocol with a null object",
        "nothing — it wires Postgres, Temporal or memory, and the null objects answer honestly",
    ),
    "which model, at what price": (
        "config.RunConfig",
        "resolved configuration",
        "its own models and price table; an unpriced call fails at startup",
    ),
    "what the model is told it is": (
        "DEFAULT_SYSTEM_PROMPT",
        "value in the composition root",
        "its own prompt, passed to build rather than edited in place",
    ),
}
"""Twelve seams, and the point is the third column.

Nine of the twelve are held by three patterns — a **versioned rule set**, a
**protocol with a null object**, and a **projection from the specification** —
and the rest are a value or a map handed to the composition root. Nothing here
is held by inheritance, and nothing by a plugin system: the variation is data or
it is a port, and both are things a build can check.

**The one to watch is `policy.CLAIM_PATTERNS`.** It reads as a versioned rule
set like the others and is the only one whose values encode a *domain's* claims
rather than a deployment's numbers — a hotel agent's claims are not a shop's
claims with different thresholds, they are different sentences about different
things. That is where the parameterised layer is most likely to fail contact
with a second domain, and it is the prediction G2.6 should test first.
"""


def seams() -> dict[str, tuple[str, str, str]]:
    """The variation points, and what holds each one."""
    return dict(SEAMS)
