# Reference Agent

Customer support for a clothing e-commerce site, built as the reference
implementation for [Clean AI Engineering](https://github.com/dataagentsai/clean-ai-engineering).

Sixteen modules, one per layer of the **AI Harness Catalog** — no framework,
because a framework owns the control loop, context assembly and failure handling,
which are the layers the catalogs exist to make visible.

## Status

**Phase A complete: all eighteen modules built, all sixteen AHC layers covered.** The agent is drivable end to end through
`entrypoint.Agent.handle()` — AHC-0010, the surface an evaluation drives —
against an in-process MCP server with a scripted model: no network, zero cost.

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
uv run pytest          # everything: the suite, plus the build checks below
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
AGENT_DATABASE_URL=postgresql://agent:local-dev-only@localhost:5433/support_agent uv run pytest

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
the agent's spec (the AOAS, in `clean-ai-engineering/drafts/examples/`). A world
file such as [`worlds/clothing.yaml`](worlds/clothing.yaml) cites that spec and
adds only the rows at t₀ and what the twin is faithful about; the loader rejects
a world that tries to declare entities or actions itself. The projection
generates an MCP server from spec plus world with no per-tool code, so a second
world is a second file.

```bash
uv run pytest tests/test_agenttwin.py
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

## Citing

Cite the release you used. Metadata is in [CITATION.cff](CITATION.cff); GitHub's
"Cite this repository" button renders it as APA or BibTeX.

## Licence

[Apache 2.0](LICENSE).
