# G0.1 — what tagging the suite found

Five reviewers tagged every test with the statements it discharges, one batch
of files each, on 11 September 2026. Their reports are kept here as evidence:
each lists every test left **untagged**, what it verifies, and the missing
statement as one *must* sentence. That list is the work G0.6 draws from.

| Batch | Files |
|---|---|
| [B1](B1.md) | escalation, tier 2, desk, flow and resilience |
| [B2](B2.md) | policy, approvals, approver, truth, omission, typed boundary |
| [B3](B3.md) | postgres, entrypoint state, trigger, identity and idempotency, contracts, config, release gates |
| [B4](B4.md) | cassette, serve, reviewer, telemetry, conformance |
| [B5](B5.md) | context budget, cost, tools, router and loop, invariants, AgentTwin, actors, break-it, golden |

## Before and after

| | Before | After |
|---|---|---|
| Test functions tagged | 91 of 396 | 195 of 400 |
| Untagged — a feature no spec requires | 305 | 126 |
| Tooling — tests of instruments | 0 | 66 |
| Unwired — tests of components the agent never calls | — | 13 |
| AOAS statements exercised | 0 / 33 | 24 / 33 |
| AAC obligations exercised | 44 / 49 | 41 / 49 |
| AHC capabilities exercised | 0 / 60 | 29 / 60 |
| Baseline items exercised | 0 / 12 | 4 / 12 |

**AAC went down, and that is the point.** Twenty tags were removed as wrong on
review — four credited an excluded release gate (AAC-0096) to replay tests; one
test's assertions were tautological; several credited obligations to code the
agent never runs. A tag that names the wrong statement reads as coverage and
verifies nothing.

## What the untagged remainder says, in one line each

- **Authentication has no spec.** Identity from a verified token and never the
  request body, tampered and expired tokens refused, a short signing secret
  failing startup — tested, required by nothing. P-OWNERSHIP rests on it.
- **Deterministic routing has no statement** — which requests skip the model,
  and that the direct route never serves a write.
- **The escalation and approval rules have no citable ids** — the cap, the
  cooldown, the facts the rules read, "a decision is final", "the approver is
  never the customer", the 24-hour validity. The AOAS states them in sections
  without identifiers.
- **Operational stores are unspecified** — checkpoint atomicity, what survives a
  restart, the reviewer's closing record that the over-escalation rate is
  computed from, and replay's matching rules.
- **Cost arithmetic is unspecified** — unknown model is an error, not free;
  money is decimal; cached input is priced apart.

## Conflicts between the specs, and between spec and code

- **AOAS lets the agent cancel on its own authority; AHC-0057 requires approval
  for every irreversible action.** Either cancellation before picking is
  reversible (it has a declared compensation), or the catalog needs a risk
  threshold, or the AOAS is wrong. A decision, not a tag.
- **The AOAS says the customer is told nothing about when a person will arrive;
  the code gives a wait estimate** from queue depth and throughput.
- **A test pins a second refund of one order under a new idempotency key**
  (`test_a_later_iteration_is_a_genuine_second_execution`) — true of the key,
  forbidden by the AOAS's `order_id` identity. The world now refuses it (1.3);
  the key's semantics and the domain's are different statements, and the spec
  should say which governs.

New defects: F-022–F-026 in [FINDINGS.md](../FINDINGS.md).
