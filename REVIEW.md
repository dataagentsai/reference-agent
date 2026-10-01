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
  validates it. AAC-0011 says "every call emits a complete trace" — complete
  against what? Nothing answers that. *(Recorded as AHC-0011, an old number;
  AHC-0011 is now "untrusted content is fenced". Corrected 2026-10-01.)*

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

→ **F-082** (recorded here as F-010, a number the cassette fix took the next
day; renumbered 2026-10-01), and our AAC-0098 discharge is thinner than the
obligation: it asserts each model is *configurable and priced*, not *evaluated*.

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

## R-013 · "Why are you saying AgentTwin does not cover the other four shapes?"

**Exposed:** an overstatement of my own, in a document I had just written.

Doc 28 scored four of five shapes ❌ and concluded *"AgentTwin only works for one
of the five"*. Measuring the package refuted it: **five MCP-bound lines out of
~984**, all in `projection.py`. `world`, `loader`, `actor`, `scenario` and
`record` contain zero. The claim contradicted `projection.py`'s own docstring —
*"one scene, many render delegates"* — which is to say I had designed the
separation deliberately and then failed to credit it a day later.

The correction is not a smaller number, it is a **different axis**. Two questions
were being conflated: the **contract face** (MCP / HTTP / filesystem — a renderer,
~40 lines) and the **world model** (rows / a git tree / a corpus — a research
question). Scored on both: 1 covered, 2 need only a renderer, 2 need a genuinely
different world model. The hard limit is real and it is not the protocol —
`entities → fields → conditions` cannot describe a file tree, and `Condition`
cannot say *"these two documents contradict each other"*.

→ Doc 28 Finding 2 rewritten. **A claim in a committed document is a defect like
any other**, and this is the first one review caught in prose rather than code.

---

## R-014 · "AgentTwin treats the agent as a box; it needs the semantics, and the agent's functional aspect"

Stated as a restatement rather than a question, and it decomposes into six stages.
Two of them do not exist.

| | Stage | Status |
|---|---|---|
| 1 | Agent is a box talking to systems | ✅ intercept at the contract face; never imports the agent |
| 2 | Mock or synthesise those systems | ✅ `projection.py` |
| 3 | **Understand semantics → generate correlated data** | ❌ **F-011** |
| 4 | **Understand the agent's functional aspect** | ❌ **nothing declares it** |
| 5 | Generate scenarios and test cases | ⚠️ cases yes, scenarios hand-written |
| 6 | Execute | ✅ `scenario.py`, world diff as oracle |

**Stage 3, measured.** 12 of 29 generated golden cases describe a world that
cannot exist — `status=pending` with `days_since_delivery=30`. Type-valid,
referentially valid, semantically impossible, because `world.py` has `Entity`,
`Field` and `Condition` and **no concept of an invariant**. → **F-011**, open.

**Stage 4 is the larger gap and was not on any list.** The world file declares
what *exists* and what is *allowed*. **Nothing anywhere declares what the agent is
for.** AgentTwin has a complete model of the environment and no model of the job —
which is exactly why stage 5 is half-built: cases generate mechanically from
conditions, while scenarios are hand-written, because a scenario needs a *goal*
and nothing knows what goals exist.

The functional model is not missing information — doc 24 has it: intents, an
action catalog, an eligibility matrix, success predicates. It is missing as
**data**. It lives in prose, so a human has to read it and type scenarios. Make it
a declaration and `intent × reachable world state → scenario + predicate`
generates, the same way conditions already generate cases.

Also raised, and the reason the box holds: *"that agent is written in A, B or C —
it does not matter, till the contracts are well defined."* Recorded as doc 28
Finding 2b. It is the mirror of the import contract we already enforce — *the
agent cannot see its simulator*, and the simulator does not look inside the agent.
Its condition: a face must be **declared and complete**, which is what MCP's
mandatory `outputSchema` buys and a schema-less REST API does not.

---

## R-015 · "You picked the wrong examples — and an agent is also software, nothing more nothing less"

Four corrections in one exchange, each sharper than the last. Written up in full
as [`29-three-jobs.md`](../DataAgents.ai/29-three-jobs.md); the parts that land on
this repo:

**The axis was wrong.** Doc 28 sorted five agents by framework — Claude SDK,
LangGraph, Bedrock — one day after establishing that the framework is the one
attribute that does not change the test surface. Redone as four *jobs*: support,
researcher, Databricks cost, multi-agent research.

**The world-diff oracle silently passes.** A researcher agent writes nothing, so
`world₀ == world₁` and every scenario succeeds. Not missing coverage — a **false
pass**, which is worse, because it ships looking like coverage. Any agent whose
output is an artifact rather than a mutation hits it. Three oracle families are
now named: world diff, provenance, bounds. We have one.

**Three world shapes, not one.** Rows-snapshot (support) is covered; typed edges
(corpus), aggregates over history (ops) and visibility/topology (multi-agent) are
not. `ref` is our only edge and it is a referential string on a field.

**We have been re-deriving solved problems.** F-011 is *constrained combinatorial
testing* — forbidden tuples, in the CIT literature for twenty years. Coherent
seeded state is *factories with traits*. The missing functional model is
*model-based testing*. The missing oracle is *metamorphic testing*, from 1998.
"Is the golden set any good" is *mutation score*. R-006 is *Jepsen*. The whole
programme is *deterministic simulation testing*. **The gap is the description
format, not the techniques** — which is a far more defensible thesis than "agent
testing is new".

**And the premise underneath:** an agent is software, nothing more nothing less.
A keyword pass over AAC's 110 obligations finds 25 naming something LLM-specific
and 85 that do not — and the clear cases (*conforms to its declared contract*,
*latency within budget*, *cost per successful task*, *graceful degradation on
provider failure*) are ordinary service engineering that would read the same in
2010. What makes an obligation genuinely agent-specific is its **cause** —
nondeterminism, natural language as interface, or instructions and data sharing a
channel — not its topic.

Also drawn out: **OpenUSD's composition arcs** are the largest unclaimed idea for
this codebase. `perturbation.py` mutates the world; layering would make world₀ a
base layer, the perturbation a non-destructive layer, the diff structural, and
perturbations portable. And `worlds/electronics.yaml` is a **fork where USD would
use a variant** — it worked, and it will not survive the tenth world.

→ Build order reset. Nothing here is fixed yet; F-011 and the layering retrofit
are the two that get dearer with every week of new code.

---

## R-016 · "Take ten agents, cover breadth"

Ten agents run through the boundary method — research with subagents, hotel
concierge, a ClickHouse cost analyst, a coding agent, a nightly remediator, an
elderly companion, a supply-chain graph agent, clinical triage, a voice IVR, and
email triage. Written up as [`31-ten-agents.md`](../DataAgents.ai/31-ten-agents.md).
Three findings came out of the **count**, not any single write-up.

**A fourth oracle family: omission.** Clinical triage's worst failure is a missed
escalation. Nothing changed, nothing was claimed, no bound was exceeded, no
falsehood was stated — so **diff, provenance, bounds and the truth oracle built
this morning all report success**. It generalises: the concierge who never said
the upgrade was refused, the ops agent that saw a runaway cluster and did
nothing. *Failing to act is the failure mode with no evidence*, and it is
currently unrepresentable here.

**Time beats edges, six to two.** Availability, series, history, accumulation and
vitals are needed by six of the ten; typed edges by two. The build order had
edges first and that was wrong.

**Only three of ten mutate as their main purpose.** The world diff — what
AgentTwin was built around — is the primary oracle for a minority. Which
retroactively justifies having built `truth.py` before the rest of the queue, and
says omission should come next rather than anything currently listed.

**And two AAC items are miscategorised.** AAC-0007 is filed as non-functional;
for a voice agent latency *is* correctness, because three seconds of silence is a
hung-up call rather than a degraded answer. An obligation whose class changes
with the shape is one the catalog is describing from a single shape's point of
view. AAC-0092 is theoretical for nine of these ten and **load-bearing** for the
voice agent — the argument for writing its test now rather than when a voice
agent turns up.

→ Next: the two release gates with no test behind them, then omission, then
temporal conditions.

---

## R-017 · "Who is the client of `handle`?"

**Exposed: nothing is.** Every caller in the repository is a test or the
scenario runner. There is no HTTP server, no CLI, no queue consumer — the agent
has a door and nothing on the other side of it.

`handle` takes `text`, `identity`, `conversation`, `run_id` and `delivery_id`.
Three of those have obvious production sources: the request body, a verified
session token, the queue's message id. **`conversation` does not.** A real client
holds a conversation id the customer presented and must load the state for it —
and cannot, because checkpoints are filed under *run* id and every turn mints a
fresh one.

That is F-006, and this question changed its status. It had been recorded as an
abstract memory problem. It is not: **it is the first thing that would stop
anyone writing the HTTP handler.** The tests never meet it because they pass the
conversation object through in memory, which is precisely the shape of blindness
R-002 first described and nobody had connected to a caller that does not exist.

The same absence explains two other open items rather than leaving them
unrelated. Nothing mints a `delivery_id`, so AAC-0076's guard has no production
source. And nothing drives concurrent load, which is why AAC-0007 is unmeasurable
rather than merely untested — five of the nine partly-proven AHC capabilities are
blocked on that single obligation.

→ **P1 is not thin. P1 is empty**, and doc 28's table understated it. Open.

---

## R-018 · "The code does not follow SOLID — the handle function is very big"

*Session of 2026-09-11.*

**Exposed:** half right, and the wrong half was the instructive one. `handle` is
23 lines. The size is in the **`Agent` class** — 481 lines (entrypoint:76–556)
doing eleven things in one method chain: delivery guard, run identity, span
attributes, the escalation hold, approval resumption, routing, per-route
dispatch, the turn note, Tier-2 escalation, recording, and persistence. The
modules each of those belongs to already exist; the entrypoint does their work
instead of delegating to it.

The protocols exist too (`contracts/protocols.py`, seven of them) and are
**declared but not used as types** — collaborators are typed `object` with
`type: ignore`, and there is no protocol for the delivery log at all. The
composition root does not own composition: `scripts/run_server.py` makes the
concrete choices and `serve.build` does its own wiring.

And because a question about shape was asked, four defects fell out that 657
passing tests had not seen: **F-018** (refund-status answered as order status),
**F-019** (the cost ceiling unreachable from the entrypoint), **F-020** (three
reply paths skip the guardrails), **F-021** (approval expiry on the wall clock).
Every one lives in the god object — which is the practical argument for single
responsibility: the defects hid where no one responsibility was anyone's.

→ A nine-step decomposition, each step green, recorded in the TODO of the spec
family. **The generator must be told this** — a spec set that does not say
"every port is an interface, only the composition root constructs" will
regenerate the same god object.

---

## R-019 · "AHC is not complete — escalation, context bloat, errors, structured output, deterministic-first"

*Session of 2026-09-11.*

**Exposed:** correct on all five, and in the direction that matters most for
regeneration: in each area **this agent is ahead of the catalog.** The ownership
lock and lapse on escalation, tool-call pair integrity under compaction, the
circuit breaker, mandatory `outputSchema`, and the deterministic router all
exist here and are required by no capability. Delete this code and regenerate
from the catalogs, and every one of them is lost.

About eighteen missing capabilities; none covered by an assurance obligation, so
none is an intentional `see_aac` deferral. Design principles are a separate
case: AHC's scope excludes them by design, so they route to the Baseline profile
(ports as interfaces — the binding swap stands on it) and the blueprint.

→ Catalog gap-filling, reverse-engineered from the modules that do the work,
now that the runtime exists to lift them from.

---

## R-020 · "Illustrate every harness capability with this agent"

*Gap list of 2026-09-25, routed 2026-10-01.*

**Exposed:** not a question but an exercise — writing the catalog's explainer
page with this agent as the running example — and it asked more than any
question here. Putting each capability next to the line of code that meets it
found two defects that reach a customer (F-062, F-063), one leak across
customers (fixed the next day in `832c38a`), a replay that had not worked for
three weeks (F-071), a profile that contradicted its own code in four places,
and about sixty places where a capability is met in part.

| Where it went | Items |
|---|---|
| Fixed, with a finding and a test | F-062 to F-075: the direct route's *not found*, order-id spellings, temperature zero, a broken transcript as a 500, the loop's refusal reason, retry numbering, the chat-widget size limit, the watch's rule-set version, the routing label, the replay script, erasure of saved replies, skips in the map, the tool-error scrub, the escalation note |
| Fixed, no defect | the gateway's model copies are now checked against the approved list (AHC-0009); `Fault.EXHAUSTED`'s wrong example; four stale texts in harness-profile.yaml (AHC-0109 trimming, AHC-0028 "no grader runs", AHC-0098 "one class of traffic", AHC-0031/0006 "no metrics", "no gateway"); the AAC-0011 citations; run_server's Temporal comment; NOT_EXERCISED's AAC-0097 |
| Already fixed before routing | the security leak (`832c38a`); a clipped answer sent as Completed (`56021f2`); the deadline never produced (`56021f2`); the policy rules blind to look-alike hyphens (`11c2bc2`) |
| Open findings | F-076 prompt label, not prompt text, in the fingerprint · F-077 a half-written record read as none · F-078 the fact check skips no-tool turns · F-079 three spec refusals missed · F-080 the compensation list contradicts the far end · F-081 the approval forms accept unknown fields · F-082 no model evaluated before approval |
| Met in part, owned and dated | harness-profile.yaml `x_shortfalls`, one entry per capability (32) |
| Not met, owned and dated | the existing `accepted_gaps`, with corrected text |
| Not owed by A6, kept here | AHC-0054 no stop-all switch, and a takeover is seen only when a message arrives · AHC-0055 only the canary has a silence alarm · AHC-0056 only refunds write a durable record before the effect · AHC-0059 no setting bounds how many changes one request may make · AHC-0066 no idle expiry, and the demo server's conversations die with it · AHC-0067 a trim records its size, not its rule or what went, and earlier constraints are not kept · AHC-0068 the owner check is at each door, not in the store · AHC-0085 the /chat reply carries no schema version · AHC-0053 the demo server's delivery claims are in memory |
| Handed back to the catalog | AHC-0011 (fixed marker with escaping: the third question has no answer), AHC-0015 and AHC-0092 (A6 owes them; the profile reads them as not applicable), AHC-0021 (the page's wording on jitter beside Retry-After) |
| Answered by the catalog, 1 Oct | AHC-0015 and AHC-0092 now state their condition (`applies_when`, AHC f21d662) and sit under the profile's `not_applicable`, with AHC-0109 (nothing summarises) and AHC-0097 (no parallel model calls; its tests were about tool fan-out and are now tagged AHC-0104 and AHC-0020). Two concerns lost their home on the way and go back to the catalog: a turn that keeps running and paying after the customer has gone, which only 0015's cancellation clause named, and only for a stream; and the number of tool calls one step may plan, which is unbounded and is not 0097's |

---

## The pattern

Seventeen questions, seventeen findings. Three were defects that would reach a
customer, and none of them was visible to the test suite at the time — because a
test is written by the person who built the thing, and asks the question they
already had.

R-013 extends that one step further. The subject was not the code but **a
document about the code**, and the error was mine, written the same day, against
a separation I had built on purpose. Prose is not audited by a test suite at all,
so nothing but a reader was ever going to catch it.
