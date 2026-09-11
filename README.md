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
```

`pytest` also runs the three build-time checks (`tests/test_build_checks.py`),
so they cannot be skipped: **strict types** (`mypy`, strict, exhaustive
matches, no ignore without its error code), **the import contract**
(`lint-imports`, 5 contracts), and **size and complexity ceilings** (ruff),
which start at today's worst value and only move down.

Durable stores need Postgres (native, not Docker):

```bash
brew install postgresql@16 && brew services start postgresql@16
createdb support_agent && psql -d support_agent -f sql/001_schemas.sql
```

Two schemas. `agent_state` is agent-owned — conversation, checkpoints,
approvals, the idempotency ledger — and AgentTwin never projects it, because it
is the oracle. `ecom` is the business world a simulation replaces, and its DDL
is the ontology: `orders.customer_id REFERENCES customers(id)` states the join
once, machine-readably, rather than repeating it in a world file.

Modules in `(parentheses)` in that contract are declared but not yet built — the
parentheses come off as each one lands, so the contract doubles as the build
checklist.

Three further contracts fail *closed*: only `llm` may import a provider SDK,
only `tools` may speak MCP, only `state` and `idempotency` may touch the
database. A new module inherits every ban without anyone remembering to add it.

## AgentTwin

`agenttwin/` twins the agent's **world**, not the agent. The agent under test is
real; its environment is the twin. The import contract forbids `support_agent`
from importing it — a system that can see its own simulator is a system whose
results mean nothing.

A world is declared in YAML — entities, the ontology, the rows at t₀, and the
**eligibility policy as data**. The projection generates an MCP server from it
with no per-tool code, so a second world is a second file.

```bash
uv run pytest tests/test_agenttwin.py
```

The proof that the premise holds: the same 34 golden cases pass against the
projected server and against a hand-written one.

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
  exercised     38/43
  passed        38
  failed        0
  NOT exercised 5
```

The unexercised five are named in [`evals/NOT_EXERCISED.md`](evals/NOT_EXERCISED.md)
with why and what would change it. There is deliberately no "not applicable"
verdict — it would be the right label for three of them, and it would also be the
label every inconvenient obligation eventually acquired.

`evals/a6_obligations.json` carries identifiers and metadata only. The normative
statement of each obligation stays in the catalog: cite, don't restate.

## Licence

Apache 2.0.
