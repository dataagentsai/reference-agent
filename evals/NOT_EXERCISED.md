# What this suite does not cover

Six of the 56 A6 obligations have no test behind them. Each is listed here with
why, and with what would have to change for it to be covered.

**This file is the point of the exercise, not an apology for it.** Under a
management-system audit an identified, owned, dated gap is conformant while an
undiscovered one is a finding — so the honest artifact and the audit-optimal
artifact are the same artifact. A conformance report that listed only passes
would imply the remainder was fine.

There is deliberately **no "not applicable" verdict** in the report. It would be
the right label for some of these, and it would also be the label every
inconvenient obligation eventually acquired. `not_exercised` stays uncomfortable
on purpose.

*State as of 2026-09-29 — 50/56 exercised, 50 passed, 0 failed.* A test that
skips (a live service not running) records nothing, so its obligation reads as
not exercised in that run rather than failed.

Since the last version of this file, `AAC-0092` (streamed output screened) and
`AAC-0014` (continuous scoring of sampled traffic) gained tests, in
`tests/test_release_gates.py` and `tests/test_watch_rules.py`.

---

## Release gates with nothing behind them

### AAC-0046 — Partial failure and compensating actions

**Why not covered.** It was, and stopped being. Its only test,
`test_failures_are_not_recorded_so_a_retry_can_reach_the_tool`, was removed in
T-062 (`8a40e85`, 2026-09-24) when the delivery claim and the idempotency ledger
became one store. Nothing noticed, because this file still said the obligation
had a passing test. A gate that loses its evidence silently is the failure this
file exists to prevent.

**What would change it.** A test tagged `AAC-0046` against the merged store: a
far end that fails partway must leave either nothing done or a compensating
action recorded, and a retry must still reach the tool.

### AAC-0051 — Tool selection accuracy, including no-tool

**Why not covered.** Offline, the model's tool choices are scripted in each
scenario's `model:` block, so the suite tests what the harness does with a
choice, never how often the choice is right. No `scored` test measures it
against a live model.

**What would change it.** A scored live eval: cases labelled with the right
tool, or no tool, run against the real provider, with a selection rate reported
the way `docs/RELIABILITY.md` reports pass^k.

### AAC-0096 — Cached responses never cross a trust boundary

**Why not covered.** No response cache exists. The cassette is a *recording*
replayed only in tests and never serves a customer; a cassette in production
would be exactly the failure this obligation describes.

**What would change it.** Any response caching, including a prompt cache keyed
on anything a customer can influence. The failure mode is one tenant's answer
served to another, and the boundary is the cache key.

---

## Not gating

### AAC-0007 — Latency within budget at expected concurrency

**Why not covered.** Every test either replays a cassette or scripts the model,
so measured latency here is the speed of a dictionary lookup. A number produced
that way would be worse than no number, because it would be quoted.

**What would change it.** Load against a live provider. `flow` already has the
fan-out limiter this would measure.

### AAC-0016 — Scheduled re-run against unchanged inputs

**Why not covered.** Live runs exist (`scripts/live_runs.py`,
`scripts/reliability.py`) but are started by hand. Nothing re-runs them on a
schedule against pinned inputs and compares the result with the previous run, so
drift in the provider's model would go unseen between manual runs.

**What would change it.** A scheduled job running the live suite on the same
scenarios and the same pinned model, failing when pass rates move beyond a
stated tolerance.

### AAC-0097 — Processing region is enforced and recorded

**Why not covered.** Provider configuration we do not currently set or assert.
The endpoint is recorded in `RunConfig` but, since T-018, deliberately left out
of the fingerprint (a gateway in front of the same provider is the same model),
and the region that served a call is recorded nowhere — so neither half is
present. *(Corrected 2026-10-01: this said the endpoint was in the fingerprint.)*

**What would change it.** A provider that offers a region parameter, and a
deployment with a reason to pin it.

---

## Exercised but not tagged A6

Separate from the above, and pointing the other way. Seven obligations have
passing tests here but the catalog lists them for other archetypes only:
`AAC-0037`, `AAC-0040`, `AAC-0043` (A4), `AAC-0062` (A7), `AAC-0078`, `AAC-0080`
(A9) and `AAC-0088` (A10). The report lists them on every run as evidence that
they apply to a tool-using agent too.

This mechanism has already changed the catalog once. It reported `AAC-0029`,
`AAC-0046` and `AAC-0047` the same way, and as of catalog 0.12.0 all three are
A6 — the change recorded as **G1**, and pinned by
`tests/test_conformance.py::test_g1_was_accepted_by_the_catalog`.
