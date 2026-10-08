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
LIB = ROOT / "packages" / "agent-harness" / "src" / "agent_harness"
"""T-019: the mechanism layer is a package of its own. Its files are named by
their path inside `agent_harness`; this agent's are named `support_agent/...`."""

AGENT = "support_agent/"
STUB = '"""Re-export stub'
"""A module left at its old path when it moved to the library. It is the
library's module under its old name, so it is not counted as either side."""

MECHANISM = {
    "__init__.py",
    "approvals/__init__.py",
    "approvals/dbos.py",  # an adapter: the Azure stack's approval binding (T-099)
    "approvals/desk.py",
    "approvals/durable.py",
    "approvals/notify.py",
    "approvals/workflow.py",
    "cassette/__init__.py",
    "channel/__init__.py",
    "config/__init__.py",
    "conformance.py",
    "context/__init__.py",
    "contracts/__init__.py",
    "contracts/failures.py",
    "contracts/human.py",
    "contracts/ids.py",
    "contracts/intents.py",
    "contracts/kinds.py",
    "contracts/model.py",
    "contracts/protocols.py",
    "contracts/reading.py",
    "contracts/requests.py",
    "contracts/results.py",
    "contracts/tools.py",
    "cost/__init__.py",
    "entrypoint/__init__.py",
    "entrypoint/ending.py",
    "entrypoint/handoff.py",
    "entrypoint/persist.py",
    "erasure/__init__.py",
    "erasure/retention.py",
    "escalation/__init__.py",
    "escalation/capacity.py",
    "escalation/dbos.py",  # an adapter: the Azure stack's escalation wait (T-099)
    "escalation/desk.py",
    "escalation/durable.py",
    "escalation/rules.py",
    "escalation/workflow.py",
    "flow/__init__.py",
    "identity/__init__.py",
    "identity/entra.py",  # an adapter: the Azure stack's identity binding (T-099)
    "identity/sessions.py",
    "llm/__init__.py",
    "llm/pydantic_ai.py",  # an adapter: the Azure stack's model layer (T-099)
    "llm/served.py",
    "loop/__init__.py",
    "loop/dispatch.py",
    "loop/ends.py",
    "loop/freshness.py",
    "loop/plan.py",
    "loop/screen.py",
    "loop/spend.py",
    "policy/__init__.py",
    "policy/reading.py",
    "policy/states.py",
    "policy/verdicts.py",
    "portal/__init__.py",
    "requests/__init__.py",
    "requests/postgres.py",
    "resilience/__init__.py",
    "reviewer/__init__.py",
    "reviewer/approvals.py",
    "reviewer/guard.py",
    "serve/__init__.py",
    "serve/feedback.py",
    "state/__init__.py",
    "state/dbos.py",  # an adapter: DBOS on PostgreSQL, under both waits (T-099)
    "state/facts.py",
    "state/file.py",
    "state/postgres.py",
    "telemetry/__init__.py",
    "telemetry/azure.py",  # an adapter: the Azure stack's telemetry binding (T-099)
    "telemetry/contract.py",
    "telemetry/counters.py",
    "telemetry/meters.py",
    "telemetry/names.py",
    "telemetry/redaction.py",
    "tools/__init__.py",
    "tools/mcp.py",
    "watch/__init__.py",
    "watch/engine.py",
    "watch/langfuse.py",
    "watch/outcomes.py",
    "watch/record.py",
    "watch/verdicts.py",
}
"""Kept whole by a second agent — and since T-019, installed by it: every file
here is in `agent_harness`, and the import contract forbids any of them to
import an agent. The escalation and approval *workflows* are here and their
*policies* are not: how an approval expires is universal, what needs one is this
shop's. `approvals/durable.py` is the same division inside Temporal: the
workflow waits for any action, and the agent's `approvals/refund.py` says which.
Each engine split out of a parameterised module (policy, Tier 2, the watch's
judging, the span contract, the vocabulary port) is here, and its values are
not."""

PARAMETERISED = {
    "support_agent/entrypoint/__init__.py",  # the turn: universal sequence, this shop's routes
    "support_agent/entrypoint/pending.py",  # universal approval flow; the refund replies are ours
    "support_agent/binding.py",  # the shape is universal; the scope names are this deployment's
    "support_agent/config/__init__.py",  # resolve and fingerprint are universal; the values ours
    "support_agent/entrypoint/promise.py",  # the gate is universal; the phrases are this voice
    "support_agent/router/__init__.py",  # the engine is universal; the intents and refusals are not
    "support_agent/router/concerns.py",  # the split is universal; VIA names this shop's intents
    "support_agent/watch/checks.py",  # the checks are universal; the vocabulary they read is ours
    "support_agent/watch/canary.py",  # the probe is universal; its cases ask this shop's questions
}
"""Code a second agent keeps and values it replaces, **not yet split**. The layer
to watch: these read as shared and are not, so the values carry a version and
the code carries a test that the values are reached. T-019 split seven of the
sixteen (below, under per-agent, is what each left behind); these nine are the
next splits, and `entrypoint/__init__.py` joined them — it was called mechanism,
and it is the turn *this shop* runs: its router, its deterministic answers, its
consent and promise gates. A generic turn runner is what a split would extract."""

PER_AGENT = {
    "support_agent/__init__.py",  # declares this shop's vocabulary, spans and rules
    "support_agent/approvals/__init__.py",  # the harness's waits beside Policy and the refund
    "support_agent/approvals/policy.py",  # the threshold and the states; the wait terms are Terms
    "support_agent/approvals/refund.py",  # a refund tool, an order's total, a policy approver
    "support_agent/conformance.py",  # names this repository's obligation manifest
    "support_agent/contracts/__init__.py",  # the harness's contracts beside Intent and OrderStatus
    "support_agent/contracts/domain.py",  # this domain's entities
    "support_agent/contracts/reading.py",  # the order-id shape and status grammar; the fold moved
    "support_agent/entrypoint/consent.py",  # which of this shop's actions a customer asks for
    "support_agent/entrypoint/direct.py",  # order status, refund status
    "support_agent/entrypoint/handoff.py",  # the harness's handoff, with this shop's words, rules
    "support_agent/entrypoint/opening.py",  # a customer's orders and queues, on opening
    "support_agent/escalation/__init__.py",  # the harness's waits beside this shop's wording
    "support_agent/escalation/rules.py",  # the Tier 2 rule set; the engine moved
    "support_agent/escalation/wording.py",  # what this shop says to its customers
    "support_agent/policy/__init__.py",  # the claim patterns and rules; the engine moved
    "support_agent/portal/__init__.py",  # the harness's portal, with this shop's page
    "support_agent/reviewer/__init__.py",  # the harness's desk, with this shop's page
    "support_agent/reviewer/page.py",  # a desk page, in this shop's words
    "support_agent/serve/__init__.py",  # the harness's edge, with this shop's chat and desk pages
    "support_agent/telemetry/spans.py",  # the spans this shop's own modules open
    "support_agent/ui/__init__.py",  # a support chat page
    "support_agent/watch/__init__.py",  # the harness's watch, with this shop's rules as defaults
    "support_agent/watch/evidence.py",  # which rule evidences which AAC id; the verdict moved
    "support_agent/watch/rules.py",  # the rule table; the judging moved
}
"""Written afresh by a second agent, from its own specification. Since T-019 this
includes what each split left behind: the values an engine in `agent_harness`
is handed, and the thin modules that hand them over."""

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

MEASURED = """Measured 2026-10-08, by extraction (T-019), before a second agent exists.

The mechanism layer is now a package, `agent_harness`, that the agent installs
and that may not import it. By lines as the files stand (`measured()`):
mechanism 14,339 of 19,320 — **74.2%** — parameterised 2,131 (11.0%), per-agent
2,850 (14.8%). The reading predicted about three quarters of this package by
line; extraction landed on it.

What moved the number, in both directions. Seven parameterised modules were
split, and each engine joined the library (the policy positions, Tier 2, the
watch's judging, the span contract, the vocabulary a check reads, the approval
wait's terms, the handoff with its wording handed in), so the mechanism share
gained what the reading had filed as parameterised. One module the reading
called mechanism was not: `entrypoint/__init__.py` is this shop's turn, its
router and gates, and it is parameterised now. And every edge the library had
into the agent became an input — a protocol, a registration at import, or a
value handed to a builder — which added lines on both sides of the line.

The prediction this does **not** test is the one that matters: how much of the
library a second agent keeps unedited. That is G2.6's, and the claims agent is
the first chance to measure it."""


def classify() -> dict[str, str]:
    return {module: layer for layer, members in LAYERS.items() for module in members}


def is_stub(path: Path) -> bool:
    return path.read_text(encoding="utf-8").startswith(STUB)


def files() -> dict[str, Path]:
    """Every classified file by its name here: the library's by their path in
    `agent_harness`, this agent's as `support_agent/...`, stubs left out."""
    out = {str(p.relative_to(LIB)): p for p in LIB.rglob("*.py") if "__pycache__" not in str(p)}
    for p in SRC.rglob("*.py"):
        if "__pycache__" not in str(p) and not is_stub(p):
            out[AGENT + str(p.relative_to(SRC))] = p
    return out


def modules() -> set[str]:
    return set(files())


def measured() -> dict[str, int]:
    """Lines per layer, as the files stand."""
    placed = classify()
    out = dict.fromkeys(LAYERS, 0)
    for name, path in files().items():
        out[placed[name]] += len(path.read_text(encoding="utf-8").splitlines())
    return out


__all__ = [
    "LAYERS",
    "LIB",
    "MEASURED",
    "PREDICTION",
    "SEAMS",
    "classify",
    "files",
    "measured",
    "modules",
    "seams",
]


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
