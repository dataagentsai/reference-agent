# Findings

What broke when we tried to break it. Recorded before being fixed, because a
report with its failures in it is the only deliverable this programme has ever
claimed — and a finding that appears already-fixed teaches nobody anything.

---

## F-001 · The agent can claim a cancellation that never happened

**Found** 2026-09-02, Phase E, by `test_a_false_cancellation_claim_does_not_reach_the_customer`.

**Severity** High. It reaches a customer, and nothing in the world records it.

### What happens

The order has shipped. The agent calls `cancel_order`, the world refuses, and the
model replies:

> *"Done — I have cancelled that order for you."*

The world diff is **empty**. Nothing was cancelled. The only place the damage
exists is the transcript — and the customer stops watching for the parcel.

### Why the existing control missed it

`policy.no_unclaimed_refund` guards exactly one action. It was written when the
refund path was built, it is correct, and it is *specific*. `cancel_order` is
equally irreversible and has no equivalent rule.

This is the shape of the defect rather than the defect itself: **a guardrail that
names an action protects that action**. The system already knows which actions
are irreversible — every `ToolSpec` carries `side_effect`, and the world declares
it — and the policy was not answerable to that list.

### Fix

`no_unclaimed_effect`, driven by the tool registry rather than by a hard-coded
name. A companion test fails if an irreversible tool exists with no claim
pattern, so the next irreversible action cannot arrive unguarded the way this one
did.

---

## F-002 · A stale read becomes a false confirmation

**Found** 2026-09-02, by `test_a_stale_read_cannot_become_a_false_confirmation`.

**Severity** High, and it is the more interesting of the two.

### What happens

The agent reads the order as `pending`, decides to cancel, and the order ships in
between — the check-then-act window, staged by `StaleRead`. The tool server
re-checks and refuses, correctly. The model then answers from **what it expected
rather than what it was told**:

> *"That order was still pending, so I have cancelled it."*

### Why this one matters more

The server did everything right. The ledger did everything right. The failure is
entirely in the gap between a tool result and a sentence, which is precisely the
region no obligation covered — this is **G2** on the gap list, recorded weeks
before there was code to demonstrate it, and now demonstrated.

It also shows why `world₁` is the oracle. Every transcript-grading eval would
score that reply as helpful, fluent and responsive. Only the diff says it is
false.

### Fix

Same rule as F-001. The claim is checked against what the tools returned, not
against what the model believed when it started.

---

## F-003 · The policy was not answerable to the world's own declaration

**Found** 2026-09-02, by `test_the_policy_covers_every_irreversible_action_not_just_refunds`.

**Severity** Structural. F-001 and F-002 are instances of it.

### What happens

The world declares two irreversible actions — `cancel_order` and `issue_refund` —
and the output policy had a rule for one of them. Nothing connected the two
statements, so the gap was invisible until an action was added and a claim about
it went unchecked.

### Fix

The completeness check is now a test rather than an intention: the set of
irreversible tools in the registry must equal the set with a claim rule. Adding
an irreversible tool without one fails the build.

---

## F-004 · The fix for F-001 blocked correct behaviour

**Found** 2026-09-02, minutes after the fix, by the Phase D gate test.

**Severity** High, and the most instructive of the four.

### What happened

`no_unclaimed_effect` matched the bare verb. So this entirely correct reply —

> *"That order has shipped, so it can no longer be cancelled."*

— was blocked, and the customer was handed to a colleague for asking a question
the agent had answered correctly.

The existing `no_unclaimed_refund` had the same latent defect from the day it was
written (*"that cannot be refunded"* would have been blocked) and **no test ever
caught it**, because every test of that rule used an affirmative sentence.

### Why it matters more than F-001

A guardrail that misses a claim fails occasionally and quietly. A guardrail that
blocks correct behaviour fails *constantly and visibly* — and is therefore
switched off, at which point it protects nothing at all.

### Fix

Every claim pattern now requires an affirmative construction — a subject and a
completion marker — rather than the verb alone. Six honest refusals and four
affirmative claims are pinned as tests, so the next pattern cannot be widened
without someone seeing which refusals it would swallow.

### What caught it

The Phase D gate, which asserts on the world diff *and* on the reply. A gate that
only checked the diff would have passed: nothing was cancelled, which was true,
and the customer was still failed.

---

## F-005 · The deterministic route was bound to one tool signature, and could raise

**Found** 2026-09-02, by the first scenario run against a *projected* world
rather than a hand-written one.

**Severity** High. It breaks the output contract on the one route nobody watches.

### What happened

`entrypoint._direct` hard-coded both the tool name and its argument name
(`order_id`). The declared world names its key field `id`, so schema validation
rejected the call — and `_direct` caught only `ToolUnavailable`, so the
`ValidationError` **escaped `Agent.handle()` entirely**.

Two defects in one place:

*Coupling.* The deterministic path assumed a tool signature. It worked for as
long as exactly one world existed, which is the definition of a latent defect.

*Not total.* AAC-0099 says the output contract holds on every route, and there is
a test asserting it — but that test drove the direct route against the
hand-written world, where the argument name happened to match. The obligation was
discharged by a case that could not fail.

### Why the actor found it and 393 tests did not

Every earlier test either drove a hand-written server or called tools directly.
This was the first time the **deterministic path** met a **projected world** —
and a scenario is what put those two together, because an actor opens with what a
customer would actually say rather than with the phrasing a test author picked to
reach the branch they were testing.

### Fix

The argument name is read from the tool's declared schema. Every failure in
`_direct` now returns a typed `Failed`, including the ones nobody anticipated —
which is the only way "the contract holds on every route" can be true rather than
tested.

---

## F-008 · Context trimming orphaned tool calls

**Found** 2026-09-02 by a probe written while auditing context handling.

**Severity** High. It is a production 400 on any conversation long enough to trim.

`assemble()` dropped messages from the middle **by index**, with no idea that an
assistant turn carrying `tool_calls` and the `tool` messages answering it are one
indivisible unit. Eight of fifty-five turn-and-budget combinations produced a
transcript where an assistant claimed a call whose answer had been removed —
which is the identical wire-format failure the first live Groq call produced:
*"Tools should have a name!"*

No test caught it because no test ran a conversation long enough to trim. The
tests that existed were about *what* was kept, not about whether what was kept
was coherent.

**Fix.** Trimming groups history into exchanges and drops whole units.
`assemble()` now checks the invariant on every call and raises
`BrokenTranscript` rather than handing a provider something it will reject, and
a Hypothesis property covers every turn count and budget: **0 orphans in 357
combinations.**

---

## F-009 · Nothing checked whether a stated fact was true

**Found** 2026-09-02, by probing nine hallucinations against the output rules.

**Severity** High, and it is a gap of *shape* rather than of care.

**Seven of nine passed.** The tool returned `status: delivered`, and the agent
could invent an order id, a status, an amount, a carrier, a count or a policy
number — or flatly contradict the tool — with nothing stopping it.

Every control that existed checked a claim about **the agent's own behaviour**.
F-001 and F-002 taught that lesson so thoroughly that it got built for, and the
other half never did: nothing checked a claim about **the world's content**.

**Fix.** `no_ungrounded_entity` — every identifier, date and amount in a reply
must appear in the structured tool results of that turn. This is possible only
because `outputSchema` is mandatory: results are structured, so the question is
set membership rather than judgement. M1, not M3, and therefore free, exact and
runnable on every reply rather than sampled.

**And a rule that was removed on the way.** Status words were checked too, and
produced *zero true positives and one false positive*: "it can no longer be
cancelled" contains "cancelled", no tool returned that status, and a correct
refusal was blocked. F-004 repeating within the same hour. Identifiers, dates and
amounts are unambiguous tokens; a status word is ordinary English that appears in
refusals and policy explanations, so grounding it needs meaning rather than
membership.

**What it still cannot catch**, pinned as a test so the limit is a fact rather
than a hope: a reply that negates a true fact, one that invents a fact with no
entity in it, and one that miscounts. All three need a judge, and a judge needs
its own validation before it can be trusted.

---

## F-011 · Twelve of twenty-nine generated cases describe a world that cannot exist

Raised in review: *"we also need to understand the semantics of those systems so
that we can generate meaningful correlated data."* Measured immediately:

```
29 generated cases
12 describe a world that cannot exist:
  open_return_request  status=pending          days_since_delivery=30
  change_address       status=confirmed        days_since_delivery=31
  cancel_order         status=picked           days_since_delivery=30
  open_return_request  status=shipped          days_since_delivery=31
  cancel_order         status=out_for_delivery days_since_delivery=30
```

An order that is `pending` has not been delivered, so it cannot be thirty days
since its delivery. Every one of these rows is **type-valid** — the int is an
int — and **referentially valid** — the customer exists. They are semantically
impossible.

**Why the generator produces them.** `AllPairs` crosses `status` against
`days_since_delivery` freely, because nothing in the world file says the two
fields are related. `world.py` has `Entity`, `Field` and `Condition`; it has no
concept of an **invariant** — a statement about which combinations of fields can
coexist. Grepping for one returns nothing.

**What it costs.** Not a wrong verdict: `evaluate()` reads the conditions and
refuses correctly, so the assertions pass. The cost is that **41% of the golden
set tests the rule against fiction.** A case that could never arise in production
cannot fail in a way production would. Worse, it hides the case that matters:
nothing generated tests `status=delivered, days_since_delivery=31` *against a
customer who already opened a return*, because coherent multi-field states are
not part of the space at all.

This is the difference between a mock and a simulation. A mock returns
well-typed answers. A simulation maintains a world that could exist — and only
the second finds the bugs that need two facts to be true at once.

**Fixed.** `Invariant` is `Condition → Condition` — material implication over the
predicate language that already existed, so no new grammar and the same
declaration reaches all three consumers:

```
world clothing-ecom -> 26 cases        (was 29)
  2 invariants pruned 13 impossible of 29 unconstrained cases
world electronics-ecom -> 26 cases
  2 invariants pruned 14 impossible of 29 unconstrained cases
```

The set got **smaller and better**. Not 29 minus 13 — the sampler spent the
freed budget on reachable combinations, so 26 coherent cases replaced 16
coherent ones. Both boundaries survived: day 30 allowed, day 31 refused.

**Constrained combinatorial testing, using the library rather than reimplementing
it.** `allpairspy` already takes a `filter_func`, which is its forbidden-tuple
support. Filtering *after* generation would have silently broken the pairwise
guarantee — the sampler would believe it had covered a pair that only ever
appeared in a row we then dropped. Passing the constraint in covers every pair
that is actually reachable. That is what the CIT literature has meant by
constraints for twenty years, and our `AllPairs` call was the 1997 version of it.

**Three consumers, one declaration** — the point of the fix, and each was tested
separately because a constraint honoured in one place and not the others is
worse than none:

- the **generator**, as forbidden tuples;
- the **loader** — a hand-seeded incoherent row now raises `InvalidWorld`, so a
  world nobody meant to write cannot silently produce verdicts;
- **perturbations** — a stale read that would leave the world impossible raises
  `IncoherentPerturbation` instead of running and reading as a finding about the
  agent.

**What forced it to be justified:** the frozen-baseline assertion (`len(CASES) ==
29`) and the committed baseline both failed. That is exactly what they are for —
the set could not shrink quietly.

**Stated limit.** One row, its own fields. `warehouse.unused → zero rows in
query_history` is cross-entity, and anything over a time series is temporal;
neither is expressible and both are named in doc 29 as domain-schema work. A
grammar that half-supported them would be worse than one that declines.

---

## F-012 · For a question, the oracle passed by being unable to see

Stage 6 decides pass or fail by comparing the world before and after. For a
question — *"where is my order?"* — nothing changes, so the comparison has
nothing to say and **every predicate written against it passes automatically**.

Not missing coverage. A **false pass**, which is worse, because it ships looking
like coverage. And not an edge case: five of the ten support scenarios are
questions, and reads are the majority of real support traffic.

**Demonstrated rather than argued**, using the two predicates the existing
scenario test already uses, unchanged:

```python
"nothing was changed":        lambda w, t: w.effects == [],
"the customer got an answer": lambda w, t: bool(t.turns[0].heard),
```

The agent tells a customer their shipped order is delivered. Both pass. The
first can only catch an effect that should not have happened; the second checks
that a string is non-empty. Neither checks whether the answer is **true**.

**And the runtime controls do not cover it either** — found while writing the
test, and it locates the hole precisely. The first lie tried was *"your order
was cancelled and has been refunded"*, and `no_unclaimed_effect` **blocked it**:
that is an *action* claim, and `CLAIM_PATTERNS` exists for exactly those. But
*"your order is delivered"* is a **state** claim, and F-009 deliberately removed
status words from grounding because they produced zero true positives and one
false positive — *"it can no longer be cancelled"* is a correct refusal
containing the word.

So the two controls divide the space and leave a gap between them:

| claim | example | caught by |
|---|---|---|
| action | "I have cancelled that" | `no_unclaimed_effect` ✅ |
| entity | "refund #R-88 on 3 Sept" | `no_ungrounded_entity` ✅ |
| **state** | **"your order is delivered"** | **nothing** |

Runtime cannot easily close it — it would need the world. **A test-time oracle
can, because AgentTwin owns the world.**

**Fixed.** `agenttwin/truth.py` compares what the reply asserts against what the
world says. Not a judge and not a metamorphic relation: those are the tools for
when correct output is *unknowable*, and here it is known — AB-10001 is
`shipped`, so "delivered" is wrong and we can simply look. Reaching for a weaker
instrument when a stronger one is available is how a suite ends up measuring its
own cleverness.

A claim needs a subject and a copula, and is discarded under a modal, a negation
or a condition — F-004's lesson pinned as twelve table cases, including the six
correct-agent sentences that must **not** trip it.

**Its limits, stated:** declared enum fields only, one named entity. A reply that
miscounts, invents an entity-free fact, or is wrong about an integer still
passes. And a conjunction yields only its first claim, which changes no verdict
since one contradiction already fails the run.

---

## F-013 · A granted refund crashes the agent

Found within minutes of building the approver actor (F-007), which is the whole
argument for building it.

`issue_refund` **as projected from the world** takes the entity's declared key,
`id`. `request_refund` is harness-local, hard-codes `order_id`, and stores its
arguments verbatim. So on resumption the agent replays `{order_id, amount}` into
a tool that declares neither, and `jsonschema.validate` raises.

**The fix already exists in the codebase and is not called here.** `_bind` maps
router arguments onto whatever the tool actually declares, and its docstring is
this defect word for word:

> *"The router knows it found an order id; it does not know what this world calls
> that field."*

It is applied on the deterministic route and **not** on the resume path. F-005
was repaired where it was found rather than everywhere its class lives — which
is the more useful lesson than the bug.

**Severity.** The grant is recorded, the elevated identity is minted, and then it
dies. Not a wrong answer: an unhandled exception, on the one path where money
moves, after a human has already said yes. Every existing approval test misses it
because they all run against a hand-written fixture server whose `issue_refund`
happens to take `order_id` — **the projected world and the test fixture disagree,
and only the fixture was ever exercised.**

**Fixed**, and the decision about *where* binding belongs turned out to be forced
rather than a matter of taste.

The instinct was to bind at **request** time, so the approval stores the exact
call it authorises — a human reviewing one thing while the system executes
another is the class of bug approvals exist to prevent. That is not possible
here: `issue_refund` requires `refunds:write`, `CUSTOMER_SCOPES` excludes it, and
the identity raising the request therefore **cannot see the tool on its
registry**. Binding has to happen at resume, after `granted_identity` mints the
elevated identity. The privilege separation that makes the gate work is the same
thing that decides this.

`_bind` gained two steps beyond its single-argument rule: drop arguments the
schema does not declare (`additionalProperties: false` would reject the whole
call for one), then fill a missing required slot from the remaining arguments by
name — `order_id` for a required `id` is the `<entity>_<key>` convention, checked
rather than assumed.

**Ambiguity raises.** Two spare values and one empty slot is a coin toss, and a
coin toss on the refund path is worse than a stop. The customer gets a typed
failure; the operator gets a sentence naming the tool, the argument it wanted and
what was on offer — instead of a `jsonschema` exception surfacing through three
nested task groups.

**What it does not fix:** the approval says ₹24,000 and the world records only
`status: refunded`, because `order` has no `amount` field. The amount a human
approved is not an amount the system moves, since the system moves no amount at
all. That is F-014, still open, and now visible rather than hidden behind a crash.

---

## F-014 · The approval threshold is checked against a number no system holds

`requires_approval` reads `args["amount"]` — whatever the model passed. The
`order` entity has no `amount` field in either world, so there is nothing to
check it against. The number arrives from the conversation and is believed.

**What saves it today is an accident.** A below-threshold request returns
`{"status": "below_threshold"}` and executes nothing at all, so a customer who
understates an amount does not get an unapproved refund — they get no refund. The
gate holds, but it holds because the cheap path is a no-op rather than because
the number was verified.

The threshold policy is therefore decorative in both worlds: ₹10,000 is compared
against an ungrounded string. The fix is a world change — `order.amount` as a
declared field — after which `no_ungrounded_entity` already grounds amounts
against structured tool results, and the gate becomes real. Open.

---

## F-015 · Malformed model output escaped the typed boundary

Found by reading AHC-0001 — *every model response crosses a typed boundary* —
and asking what we had actually built for it.

Most of it was there. `_from_wire` is the single parse site, everything above it
sees a frozen `ModelResponse`, and the import contract *"only `llm` may import a
provider SDK"* makes a second parse site impossible rather than merely
discouraged. That is the capability's `boundary_position` decision answered
correctly, and its failure mode — *"the same validation written five times with
five different opinions"* — prevented mechanically.

**The other half was missing.** Only *provider* failures became typed:

```python
except (RateLimitError, APIConnectionError, APIError) as exc:
    raise ModelUnavailable(str(exc)) from exc

response = _from_wire(raw)   # ← no error handling at all
```

Measured rather than assumed:

```
well-formed    : {'order_id': 'AB-1'}
truncated JSON : JSONDecodeError escapes complete()
prose          : JSONDecodeError escapes complete()
empty choices  : IndexError escapes complete()
```

So a model returning truncated tool-call arguments — **what a token limit does**,
not an exotic case — raised `JSONDecodeError` out of the client, past the loop's
`except ModelUnavailable`, and out of the agent unhandled. Precisely the failure
mode AHC-0001 names.

**And 495 tests never drove it.** The degenerate-input cases drive degenerate
*customer* input — empty, emoji flood, control characters. **Nothing drove
degenerate model output.** AAC-0015 was discharged on one side of a two-sided
boundary, which is F-005 for the third time: *an obligation is only as discharged
as the narrowest case that claims it.*

**Fixed**, following the capability's own `parse_failure` decision — *fail into a
declared shape and count the failures*:

- `ModelMalformed`, its own type rather than a subclass of `ModelUnavailable`,
  because the right response differs. Unavailable means nothing came back and a
  retry may work; malformed means this model on this prompt produced something
  unusable, so retrying the identical request mostly buys a second bill. **The
  no-retry rule is asserted** so it cannot drift.
- The loop returns the same declared `Failed` shape a provider outage does: a
  sentence for the customer, the detail for the operator.
- **Counted on the span** (`agent.model.malformed`) rather than logged. A parse
  failure rate that lives in a log line is a number nobody plots, and this one
  moves when a model is swapped — surviving quietly is exactly how a model change
  silently degrades.
- The `raw_retention` tension resolved toward explicability: held on the
  exception in memory, redacted by `telemetry` before recording, never persisted.

Also caught: a JSON *scalar* is valid JSON and not a call. Without an explicit
check, `"AB-1"` as tool arguments failed one layer up as a problem with the tool
rather than the model, sending whoever debugged it to the wrong file.

**The span contract caught the new attribute** the moment it was emitted —
`agent.run: undeclared attribute agent.model.malformed` — which is R-009's
contract doing the job it was built for, on the first new attribute since.

---

## F-006 · Fixed — by writing the caller that could not be written

Open since R-002, recorded as an abstract memory problem: checkpoints filed
under **run** id, a fresh run id minted every turn, so nothing could look up a
conversation by anything a customer holds.

**R-017 changed its status.** Asked who calls `Agent.handle`, the answer was
nothing — every caller was a test or the simulator. So P1 was not thin, it was
empty, and F-006 was not an abstract gap: **it is the first thing that stops
anyone writing the HTTP handler.** Three lines into `chat()` you need to turn a
conversation id into state, and there was no method that could.

**Fixed in all three stores.** `CheckpointStore` gained `latest(conversation_id)`
and `checkpoint` now takes the conversation id explicitly rather than decoding
the state to find it — a store that parsed its own payload would be coupled to
the encoding.

- **In memory:** the same bytes under both keys, in one write, because two
  writes can disagree.
- **On disk:** two atomic replaces rather than an index. A crash between them
  leaves the run record correct and the conversation pointer one turn stale,
  which is recoverable; an index pointing at a half-written file is not.
- **Postgres:** one row, one statement, both indexes — so they cannot diverge and
  no transaction is needed to hold them together. Plus
  `(conversation_id, updated_at DESC)`, which carries the sort as well as the
  filter: every read wants the newest turn, and without it each lookup reads
  every turn the conversation ever had and throws all but one away.

The live database needed migrating as well as the schema file — the tests failed
with `column "conversation_id" does not exist`, which is the correct failure and
the reason the Postgres tests exist.

**Proven over real HTTP**, not just in a unit test: two `curl` requests, the
second carrying the conversation id from the first, continuing the same
conversation.

---

## F-016 · Any customer can act on any order

**Found** 2026-09-05, by the question *"who maintains the login and its
permissions?"* — before writing any code for it.

**Severity: critical.** It is the highest-severity defect in this repository.

### What happens

```
AB-10002 belongs to: C-1042
C-9999 called cancel_order on C-1042's order -> {'allowed': True, ...}
order status now: cancelled
effects recorded: [('cancel_order', 'AB-10002')]
```

A stranger with a **perfectly valid token** and **ordinary customer scopes**
cancelled somebody else's order. Nothing was forged, nothing was escalated, no
guardrail was bypassed. Every control did exactly what it was written to do.

### Why every control passed

Because **none of them was ever asked this question.** `customer_id` appears
nowhere in `projection.py` and nowhere in `tools/__init__.py` — grep returns
nothing. The scope check asks *may this caller write orders?* and the answer is
yes. It never asks *whose order is this?*

That is the gap between two different sentences that look alike:

| | |
|---|---|
| `orders:write` says | this caller may write **orders** |
| what is needed | this caller may write **their own** orders |

The first is a permission about a *verb*. The second is a permission about a
*row*, and the token has no way to express it. This is
**broken object-level authorization** — OWASP's number one API risk, and it has
been sitting under nine layers of correct guardrails the whole time.

### Why nothing caught it

Every test, every scenario and every golden case uses **one customer**. `C-1042`
is hard-coded in the fixtures, the world seeds one customer, and the actor is
always that customer. A defect that requires two customers to observe cannot be
observed by a suite that has never had two.

The world file even declares the relationship — `customer_id: {ref: customer.id}`
is right there in the ontology — and nothing reads it at call time. The
declaration exists and no enforcement consumes it, which is the same shape as
R-012, where the ontology was decorative until the generator was made to read it.

### Not fixed

Recorded rather than repaired, on instruction — the queue is being written before
any of it is built. **T-002** carries the fix, and it is a small one: the tool
boundary already has both the caller and the row, so ownership is a comparison
rather than an architecture. The temptation to reach for an authorization service
should be resisted until the rules are more complicated than *"it is yours"*.

---

## What these seven say about the method

None of them was found by reading the code. All of them needed a world that could
be *put into a state* — shipped, then perturbed mid-run — and an oracle that was
the world rather than the reply.

Two of them were invisible to every test written before Phase E, including 367
passing ones, because those tests asked *"did the effect happen?"* and the answer
was correctly **no**. The question that found these is different: **"was the
customer told the truth about it?"**

F-004 adds the other half of that lesson: the fix for a real finding introduced a
worse defect within minutes, and what caught it was an existing test asserting on
something the fix was not thinking about. Both halves of the gate earned their
place — the diff, and the reply.

F-005 adds a third lesson, about coverage rather than correctness. AAC-0099 was
marked discharged by a passing test — and the test drove a configuration where
the defect could not appear. **An obligation is only as discharged as the
narrowest case that claims it**, and a conformance report cannot see that
distinction. Two worlds found in one run what one world had hidden for a day.

That is the argument for building the runtime before writing the obligations, and
it is now evidence rather than an assertion.
