# Reference Agent

A working customer-support agent for a clothing web shop, built in the open to
show that the AI Assurance Catalog, the AI Harness Catalog and AgentTwin together
are enough to specify, build and test a production-grade AI agent.

**Status: version 0.3.0, released 10 October 2026.**

- **Done:** the agent passes all four gates of
  [Clean AI Engineering](https://github.com/dataagentsai/clean-ai-engineering),
  last run on 11 October 2026:

  | Gate | Asks | Result |
  |---|---|---|
  | Behaviour | does it do the right thing in a simulated world | pass: 46 scenarios; 1 more needs a live model and was not run |
  | Features | does its suite exercise everything owed | pass: 171 of 171 owed statements |
  | Structure | is it built to the blueprint | pass: 16 of 16 harness layers have a module |
  | Harness | is every capability its shape owes met, or knowingly not | pass: 81 owed; 66 shown by a passing test, 12 accepted gaps, 3 not applicable |

- **Not yet:** 6 of the 56 AI Assurance Catalog obligations for a tool-using
  agent are not exercised, each named with its reason in
  [evals/NOT_EXERCISED.md](evals/NOT_EXERCISED.md); the 12 accepted gaps are
  declared in [harness-profile.yaml](harness-profile.yaml).
- No agent framework: the control loop, context assembly and failure handling
  are written out, one module per layer of the AI Harness Catalog, so that each
  layer is visible.

**Run it.** The suite reads its simulator and the specs from sibling checkouts,
so clone all five side by side. It needs [uv](https://docs.astral.sh/uv/) and Node.js:

```bash
for r in reference-agent agenttwin ai-assurance-catalog ai-harness-catalog clean-ai-engineering; do
  git clone https://github.com/dataagentsai/$r; done
(cd ai-harness-catalog && npm ci)
cd reference-agent && uv run --extra dev python -m pytest   # about 2,100 tests, 4 minutes, no network or API key
```

Tests that need a database skip when there is none (see the Postgres section
below). To re-run the four gates:
`cd ../clean-ai-engineering && uv run tools/gates.py ../reference-agent`.

**Part of a family.** Six public repositories that together specify, build and
test AI agents:

| Repository | Its job |
|---|---|
| [AI Assurance Catalog](https://github.com/dataagentsai/ai-assurance-catalog) (AAC) | what must be **true** of an AI application: test obligations |
| [AI Harness Catalog](https://github.com/dataagentsai/ai-harness-catalog) (AHC) | what must **exist** around the model call: harness capabilities |
| [AgentTwin](https://github.com/dataagentsai/agenttwin) | what an agent must **face**: a simulated world to test it in |
| [Clean AI Engineering](https://github.com/dataagentsai/clean-ai-engineering) | the specs, the four gates and the build-test-fix cycle that join the rest |
| **Reference Agent** (this repository) | the reference implementation: one agent built and tested to all of the above |
| [AgentTwin Lab](https://github.com/dataagentsai/agenttwin-lab) | the lab: a world that keeps running for days, for testing long-running agents |

**How to cite:** cite the release you used. Metadata is in
[CITATION.cff](CITATION.cff); GitHub's "Cite this repository" button renders it
as APA or BibTeX.

---

## Build status in detail

**Phase A complete: all eighteen modules built, all sixteen AHC layers covered.** The agent is drivable end to end through
`entrypoint.Agent.handle()` — AHC-0010, the surface an evaluation drives —
against an in-process MCP (Model Context Protocol) tool server with a scripted model: no network, zero cost.

Built: `contracts`, `config`, `telemetry`, `llm`, `identity`, `idempotency`,
`tools`, `context`, `router`, `loop`, `state`, `entrypoint`, `cost`, `approvals`,
`cassette`, `policy`, `flow`, `resilience`.

The layers contract has no parenthesised entries left, which is the mechanical
form of that claim — the coverage delta for this agent reads "none".

The build order follows the dependency contract upward:

```
contracts ✔ → config ✔, telemetry ✔ → llm ✔, identity ✔, idempotency ✔, state ✔
          → tools ✔, context ✔ → loop ✔, router ✔ → entrypoint ✔
```

## The architecture is enforced, not documented

`pyproject.toml` carries an [import-linter](https://import-linter.readthedocs.io)
contract. Arrows point down; siblings separated by `|` may not import each other;
`exhaustive = true` means **a new module cannot be added without being placed in
the architecture**.

```bash
uv sync --extra dev
uv run python -m pytest   # everything: the suite, plus the build checks below
                       # database tests skip if none is reachable

# The agent's own AAC coverage report (docs/AAC-REPORT.md), built by AAC's
# build-report from a junit run and the watch's saved Langfuse scores:
uv run python scripts/export_scores.py       # when Langfuse is running
uv run python scripts/aac_report.py --pytest tests --scores reports/aac/langfuse-scores.json
```

`pytest` also runs the three build-time checks (`tests/test_build_checks.py`),
so they cannot be skipped: **strict types** (`mypy`, strict, exhaustive
matches, no ignore without its error code), **the import contract**
(`lint-imports`, 5 contracts), and **size and complexity ceilings** (ruff),
which start at today's worst value and only move down.

Durable stores need Postgres. [`compose.yaml`](compose.yaml) runs it, with the
rest of the adopted stack beside it:

```bash
docker compose up -d        # Postgres on 5433, the LiteLLM proxy, Keycloak
AGENT_DATABASE_URL=postgresql://agent:local-dev-only@localhost:5433/support_agent uv run python -m pytest

docker compose --profile obs up -d       # + Langfuse     (T-016's exporter)
docker compose --profile durable up -d   # + Temporal     (T-028)
docker compose --profile channel up -d   # + Chatwoot     (T-026)
```

The heavier products sit behind profiles because the whole stack does not fit in
the ~4 GB Docker gets on an 8 GB laptop; a machine with the memory passes every
profile at once. `.env.example` lists every setting, all with local-only
defaults. A native Postgres still works (port 5432, `postgresql:///support_agent`,
the tests' default):

```bash
brew install postgresql@16 && brew services start postgresql@16
createdb support_agent && psql -d support_agent -f sql/001_schemas.sql
```

One schema. `agent_state` is agent-owned — conversation, checkpoints,
approvals, the idempotency ledger — and AgentTwin never projects it. The store
itself is not here: in production it is Saleor, which owns its own database, and
under simulation it is the world AgentTwin projects from YAML. The old `ecom`
schema is dropped by `sql/001_schemas.sql`, because nothing read it.

Modules in `(parentheses)` in that contract are declared but not yet built — the
parentheses come off as each one lands, so the contract doubles as the build
checklist.

Three further contracts fail *closed*: only `llm` may import a provider SDK,
only `tools` may speak MCP, only `state` and `idempotency` may touch the
database. A new module inherits every ban without anyone remembering to add it.

## AgentTwin

AgentTwin twins the agent's **world**, not the agent. The agent under test is
real; its environment is the twin. It is a separate repository,
[`agenttwin`](https://github.com/dataagentsai/agenttwin), checked out beside
this one at `../agenttwin` and installed as an editable dev dependency. The
import contract forbids `support_agent` from importing it — a system that can
see its own simulator is a system whose results mean nothing.

The entities, the ontology and the **eligibility policy as data** live once, in
the agent's spec (the AOAS, Application Operation Agent Spec, in `clean-ai-engineering/drafts/examples/`). A world
file such as [`worlds/clothing.yaml`](worlds/clothing.yaml) cites that spec and
adds only the rows at t₀ and what the twin is faithful about; the loader rejects
a world that tries to declare entities or actions itself. The projection
generates an MCP server from spec plus world with no per-tool code, so a second
world is a second file.

```bash
uv run python -m pytest tests/test_agenttwin.py
```

The proof that the premise holds: every golden case in
[`evals/golden/eligibility.jsonl`](evals/golden/eligibility.jsonl) passes against
the projected server and against a hand-written one.

## Findings

Phase E and the actor work found five real defects, recorded in
[`evals/FINDINGS.md`](evals/FINDINGS.md) before being fixed. Two were invisible
to 367 passing tests, because those tests asked *"did the effect happen?"* and
the answer was correctly **no**. The question that found them is different:
**"was the customer told the truth about it?"**

## Conformance

Every test run ends with a report against the **AI Assurance Catalog**, archetype
A6 (tool-using agent):

```
  exercised     50/56
  passed        50
  failed        0
  NOT exercised 6
```

That is a full run with the live services up (2026-09-29). A test that skips
because its service is down records nothing, so the obligation reads as not
exercised in that run, never as failed.

The unexercised six are named in [`evals/NOT_EXERCISED.md`](evals/NOT_EXERCISED.md)
with why and what would change it. There is deliberately no "not applicable"
verdict — it would be the right label for some of them, and it would also be the
label every inconvenient obligation eventually acquired.

`evals/a6_obligations.json` carries identifiers and metadata only. The normative
statement of each obligation stays in the catalog: cite, don't restate.

## Licence

[Apache 2.0](LICENSE).
