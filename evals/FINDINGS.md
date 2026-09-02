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

**Fix shape:** invariants belong in the world file next to conditions, because
they are the same kind of statement — the world declaring its own truth rather
than Python asserting it. `days_since_delivery > 0 implies status in [delivered,
returned, refunded]` prunes the generator's space *and* validates seeded rows
*and* constrains perturbations, from one declaration. Open.

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
