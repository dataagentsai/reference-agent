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

## The pattern

Eight questions, eight findings. Three were defects that would reach a customer,
and none of them was visible to the test suite at the time — because a test is
written by the person who built the thing, and asks the question they already
had.
