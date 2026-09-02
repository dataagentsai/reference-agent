# Review log

Questions asked in review, what each exposed, and where it went.

Kept because the pattern is the point: **every question in this session found
something**, and several found defects that 400 passing tests had not. A question
asked from outside the build is a different instrument from a test written inside
it, and this file is the record of how much that difference is worth.

Findings live in [`evals/FINDINGS.md`](evals/FINDINGS.md); this is the log of
what prompted them.

*Session of 2026-09-02.*

---

## R-001 · "Which systems is the agent talking to?"

**Exposed:** I answered with three — model, tool server, its own store — and left
out the two **human** parties entirely. The question I had actually answered was
*what does the agent call*, not *who does it interact with*.

The customer is modelled, as an actor. **The approver is not.** Doc 26's own
taxonomy says *human approver → actor + a queue*; we built the queue and never
built the actor, so the entire L14 path has only ever been tested against an
approver who is instantaneous, always available, and always answers.

→ **F-007**, open. The refund path's sole control is a human, and that human is
the least modelled part of the system, which is backwards.

---

## R-002 · "Where is the memory?"

**Exposed:** `agent_state.checkpoints` is keyed by **run id**, and a conversation
spans many runs — a fresh run id is minted per turn. So there is no way to look
up a conversation by its own id. Tests never noticed because they pass the
conversation object through in memory; a real deployment could not resume from
anything a customer would present.

→ **F-006**, open.

---

## R-003 · "Have we implemented the agent properly — how is context bloat handled?"

**Exposed:** it was not handled properly. Trimming dropped messages by index and
orphaned tool calls in 8 of 55 turn-and-budget combinations — a production 400 on
any long conversation.

→ **F-008**, fixed. Trimming now drops whole exchanges, the invariant is checked
on every call, and a property test covers 357 combinations.

Also exposed, and *not* fixed: context is bounded by **characters, not tokens**,
with no `count_tokens` equivalent and no compaction. "Deferred" and "handled" are
different words and I had been using the second.

---

## R-004 · "What are we missing by not using the Anthropic SDK?"

**Exposed:** the distinction that matters is **harness features** versus **API
features**.

Harness features — hooks, permissions, sessions, context management — we rebuilt,
and rebuilding them was the entire point of the exercise. API features have **no
substitute**: prompt caching (the largest cost lever, and the stable-prefix
ordering is already in place so it *would* work), the Batches API (offline evals
cost 2×), server-side compaction, context editing, accurate token counting.

Recorded rather than fixed: they are lost to the provider choice, not to
hand-rolling, and the reference agent's L13 numbers will read worse than a
production deployment's because of it.

---

## R-005 · "Did we set up offline and online eval, is OTel actually pushed anywhere?"

**Exposed:** four gaps, all open.

- **Nothing is exported.** `InMemorySpanExporter` only. The docstring claims *"in
  production the composition root adds an OTLP processor alongside"* — it does
  not. Langfuse Cloud was decided in D-009 and never wired.
- **Online eval does not exist.** S4 and S5 are tagged in the manifest; nothing
  runs at them.
- **AgentTwin does not consume the trace at all.** Its oracle is the world diff.
- **There is no attribute specification.** `telemetry.py` holds constants;
  nothing declares which spans *must* carry which attributes and nothing
  validates it. AHC-0011 says "every call emits a complete trace" — complete
  against what? Nothing answers that.

The last is the one worth building: a span contract plus a validator would make
M5 systematic instead of per-test.

---

## R-006 · "In what scenario does the support agent become multi-agent?"

**Exposed:** the deterministic router does not make it A7 — that is A5 in front
of A6. It becomes multi-agent when a second thing makes its own model call and
chooses its own tools: a triage agent, specialists with handoff, the approver
becoming an agent, or fan-out across orders.

And the sharp part: **the idempotency scheme breaks under delegation.** The key
is `run + step + iteration`, and a sub-agent gets its own run id — so two agents
can issue the same refund under two different keys. Same defect class as F-006,
and worse, because it would be concurrent.

Open. Not urgent while the system is A6, and it must be answered before it is not.

---

## R-007 · "How are we ensuring the agent is not hallucinating?"

**Exposed:** nine hallucinations probed; **seven passed**. Every control checked
a claim about the agent's own behaviour and nothing checked a claim about the
world's content.

→ **F-009**, mostly fixed. `no_ungrounded_entity` grounds identifiers, dates and
amounts against structured tool results — possible only because `outputSchema` is
mandatory. Negation, entity-free invention and miscounting still pass, and that
limit is pinned as a test rather than left to be discovered.

---

## R-008 · "Claude gives hooks. Without Claude, how do we implement that?"

**Exposed:** we *did* build the hook system — `policy` has four enforcement
positions and a verdict that can block. But **three of the four positions are
empty**. Only `POST_MODEL` carries rules; `PRE_MODEL`, `PRE_TOOL` and `POST_TOOL`
are declared and unpopulated.

And a capability gap: our hooks can **block**, not **transform**. A Claude hook
can rewrite content; `enforce()` returns allow or block. So "redact before it
reaches the model" is not expressible, and redaction currently lives in
`telemetry` — which covers what is *logged*, not what is *sent*.

Open.

---

## R-009 · "What is a span? What is a span contract? How does it help us?"

**Exposed:** printing one real trace to answer the question found two things
immediately.

`agent.tools.list` appears **twice** per tool call — `MCPToolClient.call()`
re-lists the whole tool surface every time. In tests that is 500µs; against a
real server it is a network round trip **per tool call**. Invisible in the code,
obvious the moment the trace is drawn.

And the span contract I had just written has a blind spot: the MCP SDK emits its
own spans, `configure()` only sets OpenTelemetry's global provider on the *first*
call, so third-party spans land in an earlier exporter. **The contract validates
what we emit, not what the trace contains.**

Both open.

---

## R-010 · "Where is eval and observability in the eight positions?"

**Exposed:** two different answers.

*Observability is correctly not a position.* Its 11 capabilities sit at P1–P6 —
everywhere — because position answers "where is a rule enforced" and telemetry
enforces nothing. It is a layer that spans the row.

*Eval found a real gap.* P7 is declared "Offline / CI … no ability to prevent
anything in production", yet AHC-0029 (a production record becomes a dataset row)
and AHC-0055 (silence is alertable) are parked there and are neither. Applying
AHC's own test — *does the same capability at a different position catch
different failures?* — CI sees curated inputs, production sees the ones nobody
imagined. Different position.

And AHC cannot say so: it deliberately refused AAC's stage axis, so **online eval
is currently unsayable**. Proposal drafted: **P9 · Production telemetry / online**.

---

## R-011 · "Where is the model router — weak model or strong model?"

**Exposed:** the layer×position grid has **L2 × P4 empty**. Model invocation has
nothing at the control loop, so *"the last two attempts failed, escalate to the
stronger model"* is unsayable. A gateway can route (L2×P2) and a library can
route (L2×P3), but only P4 can see a trajectory.

Also **L7 × P5 empty** — policy enforcement has nothing at the tool boundary,
which is the position defined as *"the last place an action can be stopped while
it is still cheap"*.

And we do not route at all: three approved models, one pinned per run.

→ **F-010**, and our AAC-0098 discharge is thinner than the obligation: it
asserts each model is *configurable and priced*, not *evaluated*.

---

## R-012 · "Don't we need an ontology and a knowledge graph?"

**Exposed:** we have an ontology — one edge — and the generator was ignoring it.
`generate_golden.py` read the *agent's* status enum and a hard-coded action list,
so a second world produced zero new cases and a fourteen-day return window was
still tested against thirty. The declaration was decorative.

**Fixed.** The parameter space is now derived from the world's own conditions,
boundaries included: `at_most: 30` yields 0, 30 and 31 because the condition says
where its edge is. A second world — `worlds/electronics.yaml`, fourteen-day
returns, cancellation one state later — generates its own cases, moves both
boundaries, and projects a server enforcing a rule nobody wrote in Python. **No
code changed.**

A knowledge graph is still not needed. What *will* be needed at the second system
is identity across systems (`same_as:`), which is a field rather than a graph
database.

---

## The pattern

Twelve questions, twelve findings. Three were defects that would reach a customer,
and none of them was visible to the test suite at the time — because a test is
written by the person who built the thing, and asks the question they already
had.
