# Why each mechanism file exists

**T-012.** `evals/reuse.py` classifies 46 files and 7,865 lines as *mechanism* —
the part a second agent in a different domain keeps unchanged. That is 77% of
this package, and until this document nothing said why any of it had to exist.

The question asked of every file: **which statement requires it?**

Three answers were expected — a statement names it, a defect produced it, or
nobody can say. A fourth turned up and is the largest single finding here.

*Verdicts are authored. `tests/test_why_each_file.py` checks only that every
mechanism file appears below, so the document cannot rot quietly as modules are
added — the same guard `reuse.py` uses for its own classification. It earned its
place on the first run by catching `flow/__init__.py`, which this document had
missed and which turned out to be the sharpest entry in it.*


**T-019, 8 October 2026: these files are a package now.** Every mechanism file
named below lives in `packages/agent-harness/src/agent_harness/` at the path it
is listed under, and the agent imports it from there; the import contract
forbids any of it to import the agent. The old paths in `src/support_agent/`
hold a re-export stub where a module moved whole. The reasons below were
written before the move and still hold — the move changed where a file is, not
why it exists. What the move *added* is in **Extracted** at the end: the engines
split out of the agent's parameterised modules, each with the statement it
answers. `entrypoint/__init__.py` below now names the library's — what an edge
drives — and the turn the reason was written for is this agent's, classified
parameterised.

---

## Summary

| Verdict | Files | Lines |
|---|---:|---:|
| **Cited** — names its statement in the file | 36 | 6,581 |
| ~~Required but uncited~~ — **closed 16 Sep**, all seven now cite | 0 | 0 |
| **Port realisation** — an implementation of a declared protocol | 4 | 361 |
| **Defect-driven** — exists because something went wrong | 3 | 693 |
| **Structural** — exists because of the import contract, not a statement | 1 | 124 |
| **Catalog gap** — no statement covers it, and that is known | 1 | 75 |
| `__init__.py` — empty package marker, 0 lines | 1 | 0 |

**Nothing is unjustified.** Every one of the 46 has a reason. That is a better
result than the exercise expected, and the interesting findings are in the shape
of the reasons rather than in an absence of them.

---

## Cited — 29 files

These name their statement in the file. Extracted mechanically, not asserted.

| File | Statements |
|---|---|
| `cassette/__init__.py` | AAC-0096, AHC-0029 |
| `conformance.py` | AAC-0054, AAC-0055 |
| `context/__init__.py` | AHC-0045 |
| `contracts/failures.py` | AHC-0110 |
| `contracts/human.py` | AAC-0020, AAC-0088, AHC-0070, AHC-0108 |
| `contracts/ids.py` | AAC-0057, AAC-0106, AHC-0074 |
| `contracts/model.py` | AAC-0008, AAC-0009, AHC-0001, AHC-0021, AHC-0022, AHC-0045 |
| `contracts/protocols.py` | AHC-0022, AHC-0074 |
| `contracts/results.py` | AAC-0009, AAC-0055, AHC-0010, P-ESCALATE |
| `contracts/requests.py` | AAC-0076, AHC-0053, AHC-0074 |
| `contracts/tools.py` | AAC-0051, AAC-0105, AHC-0074, AHC-0107 |
| `cost/__init__.py` | AAC-0008, AAC-0104 |
| `entrypoint/__init__.py` | AHC-0010 — since T-019 the surface an edge drives (`TurnAgent`); the turn itself, which cited AAC-0076, AHC-0017, AHC-0107, AHC-0108 and P-DIRECT-READS, is the agent's |
| `entrypoint/handoff.py` | AAC-0110, AHC-0070, AHC-0108, P-ESC-CAP |
| `identity/__init__.py` | AAC-0057, AAC-0106 |
| `identity/sessions.py` | AAC-0057, AHC-0099 |
| `portal/__init__.py` | AAC-0111, AHC-0099 |
| `channel/__init__.py` | AAC-0076, AAC-0111, AHC-0070 |
| `llm/__init__.py` | AAC-0009, AHC-0001, AHC-0022, AHC-0024 |
| `loop/__init__.py` | AAC-0009, AAC-0054, AAC-0055, AHC-0001, AHC-0107, AHC-0108 |
| `loop/dispatch.py` | AAC-0051, AAC-0052, AHC-0104 |
| `loop/ends.py` | AAC-0055, AHC-0025, AHC-0096 |
| `loop/freshness.py` | AAC-0113, AHC-0103, AHC-0107 |
| `loop/screen.py` | AAC-0051 |
| `loop/spend.py` | AHC-0101 |
| `policy/reading.py` | AHC-0094 |
| `policy/states.py` | AHC-0117 |
| `policy/verdicts.py` | AHC-0094 |
| `resilience/__init__.py` | AAC-0009, AHC-0005, AHC-0021, AHC-0024, AHC-0058 |
| `state/__init__.py` | AHC-0108, P-ESC-CAP, P-ESC-ONCE |
| `state/facts.py` | AHC-0108 |
| `telemetry/__init__.py` | AAC-0095 |
| `telemetry/counters.py` | AHC-0106, AHC-0111, AAC-0114, AAC-0007 |
| `entrypoint/ending.py` | AHC-0114, AHC-0111, F-020, F-026 |
| `telemetry/meters.py` | AHC-0111, AAC-0008 |
| `serve/feedback.py` | AHC-0112, AAC-0115 |
| `reviewer/guard.py` | AAC-0057, AHC-0057 |
| `approvals/notify.py` | AAC-0043, AHC-0070 |
| `reviewer/approvals.py` | AAC-0078, AAC-0057, AHC-0057 |
| `watch/__init__.py` | AAC-0014, AAC-0115, AHC-0114 |
| `watch/record.py` | AHC-0114, AAC-0060 |
| `watch/outcomes.py` | AHC-0112, AAC-0115 |
| `watch/langfuse.py` | AAC-0014, AHC-0028 |
| `telemetry/names.py` | AAC-0020, AAC-0100, AAC-0103, AAC-0104, AHC-0001, AHC-0107 |
| `tools/__init__.py` | AAC-0105, Q-TOOL-RESULT |
| `tools/mcp.py` | AHC-0034, T-002, F-017 |
| `requests/__init__.py` | AAC-0076, AHC-0053, AHC-0055, AHC-0074 |
| `erasure/__init__.py` | AAC-0117, AHC-0115 |
| `erasure/retention.py` | Q-RETENTION, AAC-0095, T-072 |
| `state/file.py` | AHC-0044, Q-RETENTION |

---

## Required but uncited — 7 files, 704 lines · **CLOSED**

**The largest finding, and the first one fixed.** A statement plainly requires each of these, and nothing
in the file says which. They are not holes in the spec set; they are holes in the
*traceability* of the spec set, and every one is cheap to close.

This matters because it is the same failure as the seven `x_untested`
capabilities (T-009): a statement believed satisfied with nothing naming the
satisfaction. When a regeneration drops one of these, nobody will notice by
reading, because reading finds no claim to check.

Each now names its statement in its own docstring. `flow/__init__.py` also
closes an `x_untested` entry — AHC-0020 was believed met, untested *and* uncited,
and one of the three is now fixed. Mechanism files citing no statement: **17 → 10**,
and the ten that remain are the four port realisations, three defect-driven
files, one structural, one known catalog gap and the empty package marker —
every one a category that should not cite a statement.

**One thing the fix exposed.** Two of the seven are governed by AOAS blocks that
have **no identifier**: `issue_refund.authority` and
`personal_data_in_conversation`. A file cannot cite what has no name, so both now
cite the identified statements that govern them instead — P-REFUND with AHC-0057,
and AAC-0095. The AOAS having unidentified normative blocks is the same
traceability gap one level up, and is worth raising against the format.

| File | Lines | The statement that requires it |
|---|---:|---|
| `approvals/workflow.py` | 149 | AOAS `issue_refund.authority.otherwise: human_approval`, and the `approval` entity with its `outcome` enum. Who may decide, and the elevated identity only a grant mints — both the spec's, neither cited. |
| `approvals/durable.py` | 261 | The same statement's *wait*: `human_approval` means a decision that may take an hour and must survive the process. Temporal's, with our rules as its validator (T-028). |
| `approvals/desk.py` | 244 | **AHC-0057** read as a boundary rather than a check: the agent's handle can ask and read, the reviewer's can decide, and no type gives one the other's power. |
| `escalation/workflow.py` | 121 | P-ESC-TTL (a queued escalation lapses), P-ESC-LAPSE (the conversation returns), P-ESC-OUTCOME (closing records an outcome, once) — as rules rather than steps, since the steps are the workflow's. |
| `escalation/durable.py` | 178 | The same three statements' *timing*. A lapse used to need somebody to remember to sweep; it is the workflow's own timer now, so the rule holds wherever the record lives (T-028). |
| `escalation/desk.py` | 224 | P-ESC-OWNS read as a boundary: the agent raises and reads, a colleague closes, and no type gives one the other's power. |
| `escalation/__init__.py` | 78 | The AOAS `policies.escalation` block in full — `on_request`, `on_condition`, and the nine statements. |
| `escalation/capacity.py` | 32 | **P-ESC-TOLD** — *"a wait only when one is measured from queue depth and observed throughput"*. This file is that clause, and the clause is the reason it exists at all. |
| `telemetry/redaction.py` | 135 | AOAS `personal_data_in_conversation`, plus the payload-capture rule in `telemetry/__init__.py`. Nothing that leaves the process carries what a customer typed. Aadhaar (Verhoeff check digit, so a random 12-digit figure is left alone) and PAN are the library's (claims-fnol-azure A12); an agent adds its own with `register`, after the library's (F-10). |
| `flow/__init__.py` | 88 | **AHC-0020** — the fan-out limiter. Found by this document's own coverage test, not by reading, and it is the sharpest case in the table: the file exists, the capability it satisfies is one of the seven in `harness-profile.yaml`'s `x_untested`, and nothing in the file names it. Believed met, untested, and uncited — three ways of not being checked, stacked. |

---

## Port realisation — 4 files, 361 lines

Justified *indirectly*: each implements a protocol declared in
`contracts/protocols.py`, which cites its own statements. A second agent keeps
these unchanged, and no statement names them individually — correctly, because a
statement should require the **port**, not a particular realisation of it.

| File | Realises |
|---|---|
| `escalation/store.py` | `EscalationStore` |
| `state/postgres.py` | `CheckpointStore`, `EscalationStore` |
| `approvals/desk.py` | `Approvals`, `ApprovalRecords` |
| `requests/postgres.py` | `Requests` |

**The one thing to notice.** Nothing declares *how many* realisations must exist.
Three checkpoint stores and one file store are a deployment's choice, and the
durability rule that governs them — added 15 September — lives in code and in no
specification. See T-010.

---

## Defect-driven — 3 files, 693 lines

These exist because something went wrong. Each names the finding; none names a
statement, because no statement asked for them.

| File | Lines | Why |
|---|---:|---|
| `serve/__init__.py` | 359 | **F-006.** Checkpoints were filed under run id and a customer holds only a conversation id, so the HTTP handler *could not be written*. The file is the proof the finding was real. |
| `reviewer/__init__.py` | 260 | **F-007.** `agent_state.escalations` held rows nothing could read. *"A queue with no surface is a queue whose entries are indistinguishable from lost."* |
| `entrypoint/switch.py` | 142 | claims-fnol-azure A13: a kill switch per agent. `Switched` wraps any `TurnAgent` and reads `agent.enabled` (a bool on the config port) before every turn; false, the turn is the paused reply — no model call and no route, the message kept on the conversation, `agent.enabled` on the turn's span, `agent.turns{result="paused"}`. Approvals and escalations waiting at the desk are decided there, untouched. |
| `entrypoint/persist.py` | 74 | Three call sites wrote the conversation and none capped it, so the one durable structure grew on every turn. |

**This is the pile worth arguing about.** A regeneration from the specs would
produce none of these, because no statement asks for them — and the agent would
be wrong in three ways that took discovery to find. Either the findings belong in
the catalogs, or the catalogs are permanently incomplete by construction and the
regeneration claim has to be stated more narrowly than "the specs are
sufficient".

---

## Structural — 1 file, 124 lines

Smaller than expected, and the surprise of the exercise.

| File | Lines | Why |
|---|---:|---|
| `contracts/__init__.py` | 124 | *"Imports nothing from this package… Everything else may depend on it; it may depend on nothing. That is the whole of its job."* A re-export barrel that exists so the layering can be enforced. |

The pending-approvals module (entrypoint/pending.py) used to be counted under *required but uncited*. Since
P-APPROVAL-STALE (T-072) it carries this shop's refund replies, so it is
parameterised mechanism now and has left this document: the Null Object pair —
`NoApprovals` / `ApprovalFlow` — is still why it has the shape it has.

**The finding is what is missing here.** T-010 was raised on the belief that a
large part of this package exists for structural reasons. It does not — **one
file**, out of forty-six. Structural pressure shows up in **how files are
split**, not in **which files exist** — the
five G0.4 extractions took one module and made four, and every one of the four
traces to the same statements the original did. That is a weaker argument for
T-010 than the one T-010 was raised with, and T-010 should be re-read in that
light: a shape specification would govern *boundaries*, not *existence*.

---

## Empty package marker — 1 file

`__init__.py` at the package root exports nothing — since T-019 it is the
library's, and carries only a docstring saying what the package is and what it
may not import. Listed so the count is complete and the coverage test passes
honestly rather than by an exemption.

---

## Catalog gap — 1 file, 75 lines

| File | Lines | Why |
|---|---:|---|
| `loop/plan.py` | 75 | Oscillation detection. The loop's own docstring: *"AAC-0055 catches hard non-termination and AAC-0054 scores path efficiency, but neither catches a loop that repeats itself and then stops in time. That gap is recorded as **G2** against the catalog."* |

A known, recorded, owned gap — the catalog does not cover it and the project
knows. The correct state for this pile to be in.

---

## What this changes

**The good news is the strongest result.** Nothing in the 77% is unjustified.
Every file has a reason, and 29 of 46 already say so in their own text.

**Three things to do**, in order of cost:

1. **Six citations.** The *required but uncited* files need a tag naming the
   statement they implement. Hours, and it removes the largest category.
2. **Decide about the defect pile.** Three files exist because of F-006, F-007
   and an uncapped write. If the regeneration claim is to hold, findings have to
   flow back into the catalogs — otherwise every regeneration reproduces the same
   three defects, and the claim is only ever true of an agent that has already
   been debugged.
3. **Re-read T-010.** Its premise was that structure is unspecified and large. It
   is unspecified and *small* — two files. The real subject is boundaries: the
   ratchets that turned one module into four, not the existence of modules.

---

## Extracted — 10 files, 1,219 lines (T-019)

Each is an engine split out of a module that mixed it with this shop's values.
The values stayed in `support_agent`; each row says which statement the engine
answers and what it is handed.

| File | Lines | Why |
|---|---:|---|
| `contracts/kinds.py` | 80 | **AAC-0055** (`TerminationReason`: why a loop stopped, never absent) and L6's side-effect classes. Split from `support_agent/contracts/domain.py`, whose intents and order statuses are this shop's. |
| `contracts/intents.py` | 45 | **AHC-0010**'s route carries an intent, and what can be asked is an agent's: `IntentName` is a string the agent's enumeration validates once it declares it (`use_intents`). |
| `contracts/reading.py` | 94 | **AHC-0089** — input shape normalised by deterministic code. The fold, and a `Vocabulary` port for what an identifier looks like and which states a sentence asserts, which the agent registers. |
| `config/__init__.py` | 44 | **AAC-0093, AAC-0008** — the ceilings that can stop a call (`Budgets`). Settings, approved models and the fingerprint are the agent's. |
| `policy/__init__.py` | 116 | **AAC-0091** — a rule that raises blocks. The positions' enforcement and its replies; the rules are the agent's, registered as each position's default (`use_default_rules`). |
| `escalation/rules.py` | 235 | **P-ESC-ONCE, P-ESC-FRESH, P-ESC-CAP** — the cooldown, the fresh count after a handback, the cap — over the world file's condition vocabulary. Its `RuleSet` has no rules; the agent's has five. |
| `approvals/__init__.py` | 93 | The waits' surface: the desk, the workflow, the reminders and the terms a wait reads (`ApprovalTerms`) — the AOAS `human_approval` wait without the refund it was first written for. |
| `telemetry/contract.py` | 311 | **AAC-0011** — complete against a declaration. The harness's spans, and `declare` for an agent's own; unchanged in purpose, split so a second agent's spans are not violations. |
| `watch/engine.py` | 120 | **AAC-0014, AHC-0028** — rules run over turns, words-needing rules skip uncaptured turns, every verdict is written. The rules, thresholds and evidence table are handed in. |
| `watch/verdicts.py` | 81 | AAC's adapter contract for a score: a verdict, the finding a failing one makes, and the obligations it evidences. The evidence *table* is the agent's. |

**What the extraction found.** Every edge the library had into the agent was a
value or a vocabulary, never control: an enum a field was typed with, a regex a
check read, a page a route served, a wording module a reply was read from, a
policy object three numbers were read off. Each became an input — a protocol, a
registration at import, or an argument with the agent's value as its default on
the agent's side. None needed inheritance in the library or a plugin system,
which is the same answer `evals/reuse.py`'s seam map gave from reading.

---

## Tier 2b: adapters chosen from configuration, checks as plug-ins

The second agent (claims-fnol-azure) is wired from its stack profile and an
environment overlay instead of `if env == ...` branches, and every reply check
sits behind one port placed by `evaluators.yaml` (architecture deck, slides
93–94). Mechanism a third agent keeps whole: no agent's words and no vendor SDK.

| File | Why |
|---|---|
| `config/registry.py` | Names to plug-ins: a port's adapter (`model: apim-ai-gateway`) or an evaluator's kind (`kind: rule`), each a lazy `module:attribute` string, so a choice imports only its own SDK. An unknown name fails at startup listing the known ones; a named-and-unbuilt one (`NotBuilt`) fails only when chosen. **AHC-0022** (substitutable by configuration), **AHC-0028**. |
| `config/profile.py` | `extends` resolved with the catalog's rules (`ai-harness-catalog/tools/resolve.js` is normative), so the composition reads which adapter each port is bound to from the stack a profile inherits. Was the reference's `evals/profile.py`, which now re-exports it; the test pinning the two implementations is unchanged. |
| `adapters/__init__.py` | The composition from configuration: a harness profile (`extends` resolved) says what each port is bound to in production, an environment overlay (`config/<env>.yaml`) says which adapter it gets here with its settings and secrets by reference only, and `ADAPTERS` maps each name the stack profiles use to a lazy factory. `plan` checks all three and builds nothing (an overlay that cannot run yet still resolves); `compose` builds in port order. An overlay leaving its profile must say `why`. Replaces `if env == ...` at a composition root. **AHC-0022**, **AHC-0004**. |
| `adapters/model.py` | The `model` port's adapters by name — `scripted`, `litellm-proxy`, `groq-direct`, `apim-ai-gateway` — each wrapped in `ResilientLLM` except the script; the model, provider and temperature stay the agent's resolved configuration. |
| `adapters/state.py` | The `state` port's adapters — `in-memory`, `postgres`, `azure-postgresql-flexible` (refuses a URL without TLS) — each a checkpoint store and request ledger on one pool, the agent's DDL run through a hook (claims-fnol-azure F-24). |
| `adapters/waits.py` | The `approval` port's adapters — `temporal-updates`, `dbos-workflows` — each the agent's handles and a person's desks, the agent's steps handed in as a hook and run on the worker's own tool connection. Its contract table found the Temporal wait expiring at once when asked with no reminder. |
| `adapters/authorise.py` | The `authorise` port's adapters, for a far end's own overlay (claims-fnol-azure A1): `asserted` (believes the agent, as the AgentTwin world does; `plan` refuses it in any overlay but a test's), `local-dev` (tokens the agent's local issuer signed for this audience, keys from its published JWKS), `entra-id` (Entra v2 tokens for this far end's app registration, `roles` mapped through `role_scopes`). **AAC-0057**. |
| `adapters/identity.py` | The `identity` port's adapters — `local-dev`, `keycloak`, `entra-id` — each the issuer, a verifier for that issuer's claim shapes, the exchange when configured, and a signer only where one exists (**AAC-0057**). |
| `adapters/telemetry.py` | The `telemetry` port's adapters — `console`, `otel-to-langfuse`, `azure-monitor-otel` — the same provider and in-memory exporter each time, differing only in where spans go beyond the process; Azure's distro chosen by the overlay, not by an environment variable. |
| `adapters/tools.py` | The `tool_runtime` port's one adapter, `mcp-client`: a URL or an in-process server, the ledger from `state`, the exchange from `identity`, and a second connection for a worker. |
| `adapters/secrets.py` | The `secrets` port: a reference in an overlay read by `environment-settings` (the environment, then a dotenv file) or `key-vault` (Key Vault's REST API with the managed identity's token; no Azure SDK). An overlay never holds a value. |
| `config/settings.py` | The `config` port (claims-fnol-azure A6): a `Key` names its source key (`payout.automatic_limit_inr`), its type (str, int, Decimal), a declared default and a check; `Settings.get` gives the current value and `refresh` re-reads. `Cached` is every adapter's product: re-read after a TTL, a bad or unreachable value keeps the last good one after the first load and stops the start on it (`SettingRefused`), an undeclared key is refused. A caller reads once per decision and records what it used (**AHC-0003**). |
| `adapters/config.py` | The `config` port's adapters — `environment-settings` (the environment, then a dotenv file re-read after the TTL), `static` (an overlay's values, for tests), `app-configuration` (App Configuration's key-value REST under one label with the managed identity's token for `https://azconfig.io`, the Key Vault adapter's pattern; no Azure SDK). One contract table holds all three (`tests/test_config_port.py`). **AHC-0022**. |
| `identity/far_end.py` | The `authorise` port: a call (operation, required scope, bearer, metadata) in, the verified `Caller` (holder from a named claim, scopes, party) out, or `CallRefused` with one reason for every failure. `Verified` is `identity.decode` with the issuer's claim shapes; `Asserted` is the simulation's belief, kept for test bindings. Authorisation from the token alone (**AAC-0057**, **AAC-0106**). |
| `identity/local.py` | The `local-dev` identity: an RS256 key made in process, so a developer's Mac verifies sessions with the production verifier and only the signer differs. The one adapter that can mint, so a sign-in page exists only where it is bound. On a Mac it is also the exchange (A1): `LocalExchange` mints a short-lived token for the far end's audience from a verified session, `LocalWorkerLogin` is the payout workflow's own login with no holder. |
| `evals/__init__.py` | The `Evaluator` port and its one request and one result (slide 93): conversation and tools in OpenAI's formats, the response with its tool calls and results, context with source ids, a golden case's expectations, meta; verdict, normalised and raw score, threshold, reason, evaluator, version, provider, cost, latency. A request lacking what an evaluator needs is `skip: missing …`, never a failure. **AHC-0028**. |
| `evals/plan.py` | `evaluators.yaml` (slide 94) read into a typed plan: kinds by registry, positions `reply`, `online`, `release` with `on_fail`, `sample`, `max_ms`, `min`. Startup refuses an unknown kind, name or field, a slow evaluator inline, and one whose needs its position cannot provide. `reply` reaches the existing hook (`policy.use_default_rules`): an erroring check fails closed exactly as a rule that raises (**AAC-0091**, **AHC-0094**); `online` samples repeatably by trace (**AAC-0014**); `release` gates a batch on a pass rate. |
| `watch/online.py` | The `online` position for the watch job: a finished turn read back from its trace (**AHC-0114**) becomes an `EvalRequest`, and every evaluator `evaluators.yaml` places online judges it, sampled per evaluator by trace id so a rerun samples the same turns (**AAC-0014**). An uncaptured turn has no response, so it is skipped, never passed. |
| `evals/guardrail.py` | The `guardrail_log` kind (claims-fnol-azure A9): reads the gateway's verdict off the reply's model calls (`Response.gateway`) and records it as a check result — `fail` labelled with the blocked category, `skip` when Content Safety was unavailable (failed open) or no call carried a verdict (locally, no APIM). Judges nothing again and never blocks: the gateway already did. |
| `evals/injection.py` | claims-fnol-azure A11 — two library rules as versioned phrase lists: `no_known_injection` (a customer's words: "ignore your instructions", "system prompt", "developer mode", role markers…) and `no_instructions_in_result` (a tool result addressing the agent: those, plus "system override", "new instructions", "ignore the above"…). Small and conservative: each phrase is anchored on words aimed at a model, and the tests hold ordinary claims and shop language to passing (F-004). |
| `evals/screens.py` | claims-fnol-azure A11 — `evaluators.yaml`'s `pre_model` and `post_tool` positions as the harness's rules (`Plan.at`): `safe_reply` ends the turn before the model; `hold_writes` lets the result in as data and adds a `PRE_TOOL` rule that re-judges the run's results and refuses any write (side effect not `read`, or undeclared) after one that fails — pure, no state: the customer's next message is a new run, the fresh confirmation; `withhold` replaces the result. Holds `Placed` and `EvaluatorFailed`, shared with `plan`. |
| `evals/rule.py` | The `rule` kind: an agent's reply rules, from its own catalogue with what each needs, and the library's golden-case checks (`tool_selection`, `must_include`), as evaluators — speed inline, provider ours, score 1 or 0. |
| `evals/stubs.py` | The kinds named and not built — `guardrail_log`, `presidio`, `azure`, `open_model` — so the YAML speaks one vocabulary now and a file that uses one fails at startup saying when it arrives (Tier 3/12). Each declares its fields, so a misspelling is refused today. |

## Azure stack adapters (T-099)

Each binds one AHC layer to the service `stacks/azure.yaml` (clean-ai-engineering)
decided for it. An adapter is mechanism: a second agent on the Azure stack keeps
it whole, and an agent on the Open Stack never imports it — each takes the
optional `agent-harness[azure]` dependency, imported lazily in the one branch
that needs it. No statement names an adapter, correctly: the statement requires
the layer, and the stack file says which service realises it.

| File | Why |
|---|---|
| `approvals/dbos.py` | The `approval` binding (`dbos-workflows`): a payout waits for a handler as a DBOS workflow, with the moves of `approvals/durable.py` — assess, wait for a decision or the expiry with its reminder, carry out under the stored key with three tries, a stale grant asked again (**P-APPROVAL-STALE**). The decision is a message our `refusal` judges inside the wait (**AHC-0057**), so no caller's rule stands in for it; the steps are the agent's own activities, handed over with `serve`. |
| `escalation/dbos.py` | The `workflow` binding for the escalation wait: raised, held, resolved by a message `escalation.refusal` judges (**P-ESC-OUTCOME**), or lapsed on the wait's own timer (**P-ESC-TTL**) — `escalation/durable.py`'s moves on DBOS. |
| `identity/entra.py` | The `identity` binding (`entra-id`): the existing RS256 verifier pointed at the tenant's v2.0 issuer and key set, reading Entra's two differing claims (`uti` for the session, `scp` as a space-separated string, `roles` for a workload's own token), and the `Exchange` port as the on-behalf-of grant, with a client secret or a federated client assertion so a managed identity needs no secret. Failures in the port's kinds (**AHC-0110**): 5xx, 429 and timeouts unreachable, `invalid_grant`/`consent_required` refused, a wrong app registration misconfigured. Authorisation still from the token alone (**AAC-0057**). Plain httpx, no MSAL. `EntraClientCredentials` is the payout workflow's own login on Azure (A1): an app token for the far end, its roles and no holder. |
| `llm/pydantic_ai.py` | The `model` binding's model layer (`x_model_layer: pydantic-ai-direct`): Pydantic AI's direct model requests behind `LLMClient` (**AHC-0022**), to Groq directly, APIM in front of Groq, or Azure OpenAI behind APIM. The port's messages, tools, calls and usage translated at the edge; the typed boundary (**AHC-0001**) and the failure kinds (**AHC-0110**) the same as `GroqClient`'s, so `ResilientLLM` retries what is transient and nothing else. The only module that may import `pydantic_ai`. |
| `llm/gateway.py` | claims-fnol-azure A9 — what a gateway in front of the provider said about one call. APIM's `x-content-safety` header (its Content Safety verdict) was dropped at the client; both clients now put the declared signals (`SIGNALS`) on `ModelResponse.gateway` and on the `gen_ai.chat` span, the refused call's too. `GroqClient` reads the raw response; Pydantic AI returns no headers, so its HTTP client gets a response hook (`listening`) that writes into the call's own `heard()` dict. The watch reads them back (`recorded`) for the `online` position. |
| `llm/served.py` | **T-018** — who serves the model behind a route, as the route can say. Split from `llm/__init__.py` when a second provider client needed the same answers; unchanged in purpose. |
| `telemetry/azure.py` | The `telemetry` binding (`azure-monitor-otel`): when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, the Azure Monitor distro builds the providers with this harness's processors inside them, so the `agent.*` spans, the span contract (**AAC-0011**) and the in-memory exporter the evals assert against are unchanged. Unset, nothing differs. |
| `state/dbos.py` | What both DBOS waits share: launch on PostgreSQL, the clock read as a step so a recovered wait sees the moment it first read, and the ballot box — a decision sent as a message, judged inside the workflow, answered through an event the sender waits on. Kept apart so neither wait carries a copy, and so `dbos` is imported by three modules and no others. |

## Tier 4a: our own approval and escalation records (claims-fnol-azure A3)

A far end that must check a payout was granted read the grant from the wait's
engine (a DBOS workflow event), so the money path depended on DBOS's storage
format. The record is ours now: a port, written by the wait in the step that
moves its state, read by the far end with no engine in sight.

| File | Why |
|---|---|
| `contracts/records.py` | The records and their ports: `ApprovalRecord` and `EscalationRecord` as the deck's tables (c-L5-pgdesign) with the code's state names and the mapping to the deck's; `args_digest`, sha256 over canonical JSON of the action, arguments, whose it is and the key it runs under, so a grant covers one call; `refusals`, the far end's check that a record is granted, unexpired and has this call's digest (**AHC-0057**). `ApprovalRecordReader` is all a far end is given (A4). |
| `state/records.py` | The stores realising those ports: `InMemoryRecords` and `PostgresRecords`, each an upsert keyed by the wait's id, so a step re-run after a crash writes the same row again and nothing else. The DDL is the agent's (F-24): `sql/002_records.sql` here, `002_records.sql` in claims-fnol-azure. |
| `adapters/records.py` | The `records` port's adapters by name — `in-memory`, `postgres` — built before `approval`, which hands the store to its waits. |
