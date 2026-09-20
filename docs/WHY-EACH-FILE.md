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
| `contracts/tools.py` | AAC-0051, AAC-0105, AHC-0074, AHC-0107 |
| `cost/__init__.py` | AAC-0008, AAC-0104 |
| `entrypoint/__init__.py` | AAC-0076, AHC-0010, AHC-0017, AHC-0107, AHC-0108, P-DIRECT-READS |
| `entrypoint/handoff.py` | AAC-0110, AHC-0070, AHC-0108, P-ESC-CAP |
| `idempotency/__init__.py` | AHC-0074 |
| `identity/__init__.py` | AAC-0057, AAC-0106 |
| `identity/sessions.py` | AAC-0057, AHC-0099 |
| `portal/__init__.py` | AAC-0111, AHC-0099 |
| `channel/__init__.py` | AAC-0076, AAC-0111, AHC-0070 |
| `llm/__init__.py` | AAC-0009, AHC-0001, AHC-0022, AHC-0024 |
| `loop/__init__.py` | AAC-0009, AAC-0054, AAC-0055, AHC-0001, AHC-0107, AHC-0108 |
| `loop/dispatch.py` | AAC-0051, AAC-0052, AHC-0104 |
| `loop/freshness.py` | AAC-0113, AHC-0103, AHC-0107 |
| `loop/screen.py` | AAC-0051 |
| `loop/spend.py` | AHC-0101 |
| `resilience/__init__.py` | AAC-0009, AHC-0005, AHC-0021, AHC-0024, AHC-0058 |
| `state/__init__.py` | AHC-0108, P-ESC-CAP, P-ESC-ONCE |
| `state/facts.py` | AHC-0108 |
| `telemetry/__init__.py` | AAC-0095 |
| `telemetry/counters.py` | AHC-0106, AHC-0111, AAC-0114, AAC-0007 |
| `entrypoint/ending.py` | AHC-0114, AHC-0111 |
| `telemetry/meters.py` | AHC-0111, AAC-0008 |
| `serve/feedback.py` | AHC-0112, AAC-0115 |
| `watch/__init__.py` | AAC-0014, AAC-0115, AHC-0114 |
| `watch/record.py` | AHC-0114, AAC-0060 |
| `watch/outcomes.py` | AHC-0112, AAC-0115 |
| `watch/langfuse.py` | AAC-0014, AHC-0028 |
| `watch/canary.py` | AHC-0113, AAC-0116 |
| `telemetry/names.py` | AAC-0020, AAC-0100, AAC-0103, AAC-0104, AHC-0001, AHC-0107 |
| `tools/__init__.py` | AAC-0105, Q-TOOL-RESULT |
| `trigger/__init__.py` | AAC-0076, AHC-0053, AHC-0055 |
| `trigger/log.py` | AAC-0076, AHC-0053 |
| `trigger/durable.py` | AAC-0076, AHC-0053, AHC-0102 |

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
| `entrypoint/pending.py` | 119 | The other half of `issue_refund.authority`: what a turn does with a decision made since the last one. |
| `escalation/capacity.py` | 32 | **P-ESC-TOLD** — *"a wait only when one is measured from queue depth and observed throughput"*. This file is that clause, and the clause is the reason it exists at all. |
| `telemetry/redaction.py` | 26 | AOAS `personal_data_in_conversation`, plus the payload-capture rule in `telemetry/__init__.py`. Nothing that leaves the process carries what a customer typed. |
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
| `idempotency/postgres.py` | `IdempotencyLedger` |

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

`entrypoint/pending.py` is counted above under *required but uncited* and belongs
here too: a statement puts it there, and the Null Object pair — `NoApprovals` /
`ApprovalFlow` — is why it has the shape it has. Existence and boundary are
different questions and this file is the clearest place they come apart.

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

`__init__.py` at the package root is zero lines and exports nothing. Listed so
the count reaches 46 and the coverage test passes honestly rather than by an
exemption.

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
