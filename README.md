# Reference Agent

Customer support for a clothing e-commerce site, built as the reference
implementation for [Clean AI Engineering](https://github.com/dataagentsai/clean-ai-engineering).

Sixteen modules, one per layer of the **AI Harness Catalog** — no framework,
because a framework owns the control loop, context assembly and failure handling,
which are the layers the catalogs exist to make visible.

## Status

Sixteen of eighteen modules are built. The agent is drivable end to end through
`entrypoint.Agent.handle()` — AHC-0010, the surface an evaluation drives —
against an in-process MCP server with a scripted model: no network, zero cost.

Built: `contracts`, `config`, `telemetry`, `llm`, `identity`, `idempotency`,
`tools`, `context`, `router`, `loop`, `state`, `entrypoint`, `cost`, `approvals`,
`cassette`, `policy`.
Declared but not yet written: `flow`, `resilience` — see `TODO.md`.

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
uv run lint-imports    # 4 contracts
uv run pytest
```

Modules in `(parentheses)` in that contract are declared but not yet built — the
parentheses come off as each one lands, so the contract doubles as the build
checklist.

Three further contracts fail *closed*: only `llm` may import a provider SDK,
only `tools` may speak MCP, only `state` and `idempotency` may touch the
database. A new module inherits every ban without anyone remembering to add it.

## Licence

Apache 2.0.
