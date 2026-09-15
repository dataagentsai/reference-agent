# TODO

Work identified by review questions and **not yet started**. Nothing here is in
progress; this is the queue.

Findings that are already defects live in [`evals/FINDINGS.md`](evals/FINDINGS.md),
and the questions that produced them in [`REVIEW.md`](REVIEW.md). This file is for
work that is *missing* rather than *wrong* — the difference matters, because a
missing capability has no failing test to point at and will otherwise be
forgotten.

Each item names **what** is missing, **why it matters**, and **where** it would
land, because an item that says only "add X" gets re-argued every time it is
read.

---

## T-001 · Nothing happens when the chat opens

**Status** Not started. Raised 2026-09-05.

**What is missing.** The customer clicks *Talk to our AI agent*, a box opens, and
it is empty. There is no greeting, no list of their orders, nothing about work
already in flight. The only route that does anything is `POST /chat`, and it
requires text — so **a session opening is not expressible at all**: it is an
entry point that is not a message.

**Why it matters.** Three reasons, in order of weight.

*The customer has to know their order number.* Every scenario in the suite opens
with a customer who conveniently types `AB-10003`. Real ones do not have it to
hand, so the first two turns of every real conversation are spent establishing
which order — turns that cost money and that a list of three orders would have
skipped entirely.

*Work already in flight is invisible.* A refund waiting on a colleague, a return
part-way through — the agent holds all of it and the customer sees none of it
until they ask. `pending_approval_id` is already on the conversation and nothing
surfaces it.

*It is the cheapest possible turn and we are not taking it.* Listing somebody's
own orders is a database read. Greeting them by name is a string. **Opening a
chat should cost zero model calls**, and a product that generates its greeting
with the model pays for every abandoned open — which is most opens.

**Where it lands.**

- `serve` — a new `GET /session` returning who you are, your recent orders, and
  anything outstanding. Same identity check as `/chat`; the check being easy to
  forget on a read-only route is exactly why it needs its own test.
- `ui` — fetch it on load and render it; make the orders clickable so the
  customer picks rather than types.
- The world already has everything needed. No new tool: `get_order` exists, and
  a `list_orders` action would be four lines of YAML.

**What it must not become.** A model call. The router's whole point is that a
question with a deterministic answer never reaches the model, and *"what are my
orders"* is the most deterministic question there is.

**What to be careful of.**

- *Another customer's orders.* The identity check on a read-only endpoint is the
  one people skip.
- *A greeting that claims something untrue.* "Your refund has been processed"
  when it has not is the same failure class the truth oracle exists for, and it
  would now happen **before the customer has typed anything**.
- *Stale data.* Orders listed at open, acted on a minute later — the same
  check-then-act window `StaleRead` already models, moved to a place nothing
  currently tests.

**AgentTwin needs it too.** A scenario begins with an actor saying something.
There is no way to express *"the customer opened the chat and saw this"*, so the
opening state a real conversation starts from cannot be simulated. Both sides
have the same gap.

---

## T-002 · Nothing issues or maintains logins, and the permission model is the wrong shape

**Status** Not started. Raised 2026-09-05. **Carries the fix for F-016 (critical).**

→ **Designed in full: [`docs/DESIGN-auth.md`](docs/DESIGN-auth.md).** The summary
below is a pointer; the design is the document.

**What exists today.** `ident.mint()` signs a token in a script. There is no user
store, no login, no password, no expiry policy anyone administers, no way to
revoke, and no way to grant one customer something another does not have —
`CUSTOMER_SCOPES` is a frozenset constant in the source.

**Three separate problems, and they need different answers.**

### 1 · The signature is symmetric — and that is the wrong shape for a token

`ALGORITHM = "HS256"`. One shared secret both signs and verifies, so **anything
able to check a token is also able to forge one**. The agent process holds it, so
a read of that process's memory or environment yields the ability to mint a
session for any customer.

Production wants asymmetric: the issuer signs with a private key it never shares,
and the agent verifies with a public key it fetches. A compromised agent can then
still read tokens and cannot write them. This is a small change — `verify()`
swaps a shared secret for a JWKS lookup — and it arrives free with any real
identity provider.

### 2 · Nothing issues the token

There is no login. Whichever provider is chosen replaces `mint()` entirely, and
the agent keeps only `verify()`.

### 3 · The permission model cannot express the rule that actually matters

This is the important one, and F-016 is its consequence.

`orders:write` says *this caller may write orders*. The rule the business needs is
*this caller may write **their own** orders*. The first is a permission about a
**verb**; the second is about a **row**, and a scope list has no way to say it.

Fixing that is **not** a job for an authorization service. The tool boundary
already holds both the caller's identity and the row it is about to act on, so
ownership is a comparison. The world file even declares the relationship —
`customer_id: {ref: customer.id}` — and nothing reads it at call time, exactly as
the ontology went unread until R-012 made the generator consume it.

A policy engine earns its place when the rules stop being *"it is yours"*:
household accounts, a partner acting for a customer, an agent acting for a
partner. Reaching for one now would add a network hop to answer a field
comparison.

**Open source, and what each is for.**

*Issuing tokens and running a login* — **Keycloak** (the incumbent; heavyweight,
Java, its own database, an admin UI that already does everything), **Zitadel**
(Go, multi-tenant by design, modern), **Authentik** (Python, friendlier admin),
**Ory Hydra** with **Kratos** (API-first, no UI, most composable and the most
assembly required). Any of them gives asymmetric signing and JWKS, so problem 1
resolves as a side effect of solving problem 2.

*Fine-grained authorization, later* — **OpenFGA** or **SpiceDB** for
relationship rules in the Zanzibar style, **Cedar** or **OPA** for policy as
code, **Casbin** if it should stay in-process. None of these is needed to fix
F-016.

**Where it lands.** `identity` (verify against JWKS rather than a secret), the
tool boundary (the ownership check), and the world file, which may want to say
*which* field carries ownership rather than having the agent assume `customer_id`.

**AgentTwin needs it too, and this is why the defect survived.** Every test,
scenario and golden case uses one customer. `C-1042` is in the fixtures, the
world seeds one customer, and the actor is always that customer. **A defect that
takes two customers to see cannot be seen by a suite that has never had two.**
A second seeded customer and one hostile actor would have caught F-016 on the day
the tool boundary was written.

---

## T-003 · Deduplication only works for one process — and the answer is mostly to adopt, not to build

**Status** Not started. Raised 2026-09-05, and **revised the same day** after the
right question: *why are we writing any of this?*

**The defect in the plan, first.** The module currently claims the durable version
is *"a swap rather than a redesign."* That is wrong. `settle` runs in a `finally`,
which does not run when a process is killed outright.

- *In memory*, the dictionary dies with the process. Nothing is stranded — it is
  self-healing **by accident**.
- *Durably*, the `in_flight` row outlives the process that wrote it and **nothing
  will ever settle it.** Every redelivery then gets `OverlappingRun`, forever.
  One crash permanently wedges that message.

A durable claim therefore needs an expiry, which the in-memory one does not. That
is the kind of thing normally discovered in production.

---

### But the real answer is to write much less of it

**1 · Make the effect idempotent where the state lives.** The cleanest option by
a distance, because it removes the coordination problem rather than solving it.

```sql
UPDATE orders SET status = 'cancelled'
 WHERE id = %s AND status IN ('pending','confirmed');
-- rowcount 1 = we did it.  rowcount 0 = somebody already did.
```

One statement, atomic, no lock, no lease, no claim table, no stuck rows. A
duplicate becomes *harmless* instead of *prevented*, and harmless survives
crashes that prevention does not.

**2 · Let the queue do it, if a queue is delivering.** SQS FIFO
(`MessageDeduplicationId`), Azure Service Bus duplicate detection, Pub/Sub
exactly-once, Kafka's idempotent producer. **Configuration, not code.**

The catch nobody mentions: a queue deduplicates *its own* deliveries inside *its*
window, and only when the **producer** attaches a stable id per message. So the
id is still the thing that matters — the queue is just somewhere to check it.
**Whoever mints the id owns the correctness.** Our browser mints one per message
rather than per attempt, which is the part that is already right.

**3 · If durable multi-step execution is needed — and an agent needs it — adopt
it rather than build it.** Temporal, Restate, DBOS, Inngest. Deduplication by
workflow id is built in, a crash resumes where it stopped, and a workflow can
sleep for an hour waiting on a human and wake up correctly.

That last clause matters more than the deduplication. **Three modules here exist
largely because we lack durable execution:**

| module | what it re-implements |
|---|---|
| `trigger` | run-once semantics |
| `state` | checkpoint and resume |
| `approvals` | a long wait that survives a restart |

Adopting Temporal would subsume most of all three. For a product that is
straightforwardly the right call.

**4 · Only if none of the above applies**, the claim table — `INSERT … ON CONFLICT
DO NOTHING RETURNING`, so the unique constraint provides the mutual exclusion,
plus a `claimed_at` expiry so a killed process cannot wedge a message.

---

### What the usual infrastructure does and does not do

**Load balancer — no, and it is the *cause*.** It is what puts two copies of the
agent behind one address, which is precisely why an in-memory dictionary stops
working. It has no concept of a duplicate.

**API gateway — less than people expect, and it may make things worse.** Gateways
do authentication, rate limiting, routing. Response caching accidentally
deduplicates identical reads and does nothing for writes. And **a gateway that
retries on a timeout is a duplicate *generator*** — a large share of duplicates in
real systems are manufactured by the infrastructure meant to add reliability.

**Queue — yes, genuinely**, subject to the producer-id caveat above. Note it also
changes the product: a queue implies the customer does not get an immediate
answer.

---

### Does read-before-write in MCP solve it?

**Partly, and it is worth being precise about how partly**, because this is the
reason the duplicate in our own test did no damage.

`cancel_order` re-reads the row and re-checks the conditions, so a second attempt
finds the order already `cancelled` and refuses. **The world stopped it, not the
harness** — and a control that works by accident of the business rules is worth
naming as such.

Three things it does not do.

*It is not atomic.* Read-then-decide-then-write is the classic check-then-act
race: two processes can both read `pending`, both conclude "allowed", and both
write. The window is small, not zero — and it is exactly what the `StaleRead`
perturbation already simulates. The fix is to make the write itself conditional,
as in option 1 above; then the check and the act are one statement.

*It only works when the effect destroys its own precondition.* Cancelling works
because a cancelled order cannot be cancelled. **Sending an email, charging a
card and calling a webhook have no state to re-read**, and for those, read-before-
write offers nothing at all.

*It protects the effect, not the conversation.* The second run refuses correctly
and the customer may still be told something confusing or contradictory. The
world is safe; the reply is not.

So: a genuine second line of defence, and not a substitute for an identifier. It
turns *"a duplicate causes harm"* into *"a duplicate causes a confusing reply"*,
which is an improvement and not a fix.

---

### Why this repository hand-rolls it anyway

Deliberately, and it should be said plainly rather than defended. This is a
**reference implementation**, and its whole job is to show what a harness must
contain. If the answer to *"how do you guarantee once"* is *"Temporal does it"*,
a reader learns nothing about what was needed — and the catalogue's own output is
supposed to be the **coverage delta**: *you chose X, here are the N things X does
not give you*. That sentence cannot be written by someone who never held the
problem.

**For a real product this trade is the wrong way round.** Adopt durable
execution, let the queue deduplicate, make the effects idempotent at the far end,
and delete `trigger` entirely.

**Where it lands.** Option 1 in the projected world and the real shop; option 3 as
a documented alternative binding rather than a rewrite. The in-memory log stays
for tests, with its docstring corrected — the durable version is a **superset**,
not a swap.

**AgentTwin needs it too.** Nothing runs two agents against one world, so a defect
needing two processes cannot be seen — the same shape as F-016 needing two
customers.

---

## T-004 · An Anthropic adapter at L2 — the loop stays ours

**Status** Not started. Raised 2026-09-05.

**What this is not.** Not adopting the Claude Agent SDK, and not giving up the
hand-written loop. Those are L4 decisions. This is L2: which client the adapter
wraps. `anthropic` brings **no loop** — you call `client.messages.create()`
inside whatever loop you already have, which is the ordinary case rather than a
workaround.

**Why it is one module.** The import contract already says *only `llm` may import
a provider SDK*, enforced by import-linter on every run. So the blast radius of
this change is `llm/__init__.py` and nothing else — the contract was written for
precisely this.

**Three open items it closes at once**, which is what makes it worth doing:

*The context budget is measured in characters.* Left over from F-008 and recorded
as "deferred and handled are different words." `client.messages.count_tokens()`
makes it tokens, which is what every budget in the system actually meant.

*Offline evals pay full price.* The Batch API is half, asynchronous, and an eval
suite is exactly the latency-insensitive workload it exists for.

*Prompt caching is unreachable.* R-004 noted the stable-prefix ordering is
**already in place**, so `cache_control` on the system block would work on the
first attempt. An OpenAI-shaped request has no field to carry it, so this is not
a matter of effort — the shim structurally cannot.

**And two things it would make newly possible**, both of which land on layers we
already own:

*Mid-conversation system messages.* An operator instruction appended to
`messages` that does not invalidate the cached prefix, and is the
injection-safe operator channel. That is L7's `PRE_MODEL` position — one of the
three declared and empty ones (R-008).

*Server-side compaction and context editing.* The long-conversation problem
answered above the harness rather than inside it, where `context` currently
trims by hand.

**What it does not change.** The loop, the router, the tool boundary, the policy
positions, the oracles, the world. All of L4 stays exactly as written, which is
the point: **the L2 and L4 decisions are independent**, and conflating them is
how a team adopts an entire harness in order to obtain prompt caching.

**Cost note.** This repository's standing constraint is free hosted open-weight
providers. An Anthropic adapter would sit *alongside* the Groq one rather than
replacing it — `LLMClient` is already a protocol with three implementations, so a
fourth costs nothing and the resolution seam decides which runs.

---

## T-005 · Carry the idempotency key to the far end

**Status** Not started. Raised 2026-09-05. **Carries F-017.**

Today the key is minted, used for a local lookup, and thrown away. The contract's
own docstring says it is *"carried to the downstream system"* and it is not.

**What that leaves unprotected**, in the docstring's own words: the call
succeeded, the response was lost, and from this side that is indistinguishable
from failure. Our ledger recorded nothing, so the retry goes out again. **Only
the party that applied the effect can tell the difference** — and only if we told
it the action's name.

**Both halves are needed.**

*Ours:* the key goes in the request. Either an argument the projected tool
declares, or MCP request metadata. Which calls need it is **derivable** — the
world already declares each action's side-effect class, so it is a rule rather
than a list somebody maintains.

*Theirs:* the far end must actually use it. The conditional write from T-003 is
the cheap version; storing the key and refusing a repeat is the complete one.

**Do this with T-003**, not separately. They are the same idea at two distances —
make the effect safe to repeat, and give the repeat a name the far end
recognises.

## T-006 · A customer's own conversations cannot be found

**Status** Not started. Raised 2026-09-15. **Needs T-002 first.**

**What is missing.** A returning customer cannot be given back anything. Close
the browser and `conversationId` — a `let` in the page, not `localStorage` — is
gone, so the next message carries no id and `_conversation_for` mints a fresh
conversation. Six past conversations, two of them with something still open, and
nothing connects them to the person in front of us.

Two separate questions are hiding in that, and only one of them is close.

*"What do I have open?"* — nearly reachable. `approvals` and `escalations` both
carry `customer_id NOT NULL` already. What is absent is a **query**: the
protocols offer `open_for(conversation_id)` and `pending()`, which are the
agent's view and the reviewer's view. Neither is the customer's.

*"Continue the one about AB-10003."* — the data exists and is unreachable.
`facts.records` is precisely *which identifiers this conversation touched*, and
it is deliberately bounded: a customer who asks about the same order nine times
costs one entry, and nothing in it is ever dropped by age. But it lives inside
the `state` blob, and `agent_state.checkpoints` has no `customer_id` column at
all — the customer is *in* the row and cannot be selected on. The tables already
disagree about this: two of the three treat a customer as a first-class thing
and the third does not.

**Why it matters.** It is T-001's third reason with a record behind it. A
customer who raised `esc_7`, closed the tab, and came back has an escalation
still `queued` in Postgres, findable by conversation id, that nothing will ever
ask for. They must quote the reference themselves — assuming they wrote it down
— which is the state `HandoffDesk` already names as the failure worth avoiding:
*an escalation that made the customer repeat everything is the moment an
assistant becomes worse than no assistant.*

**Where it would land**, in the order the blockers allow:

1. `customer_id` on `agent_state.checkpoints`, with an index. One migration, and
   it unblocks every version of this.
2. `open_for_customer(...)` on `ApprovalStore` and `EscalationStore` — the
   greeting T-001 wants, and the cheapest half.
3. A `conversation_records(conversation_id, record)` index written beside the
   checkpoint, so *"the chat about AB-10003"* is a join rather than a scan of
   every blob.
4. Resume by carrying **`facts`, not messages.**

**Step 4 is the one to argue about before building.** Prepending an old
transcript is the obvious implementation and the wrong one: the assembly window
is 24,000 characters and trims whole exchanges from the middle, so an old
conversation pushes out the turns the model is actually answering.
`facts.as_handoff()` already exists and is what a *human* colleague is given —
assembled from the record, never summarised from the transcript (AHC-0108,
AHC-0070). Resuming should mean the agent knows AB-10003 was returned and a
refund is pending, not that it has forty old messages.

**And it is a disclosure surface.** Pulling a past conversation means the agent
knows things this session's customer never said. `_conversation_for` already
refuses a mismatched owner with a 404 rather than a 403, because confirming the
id exists tells an attacker their guess was right; the same check has to hold on
every new path, and a shared device or an impersonated session is where it gets
tested.

## T-007 · Every scenario runs once, so nothing measures reliability

**Status** Not started. Raised 2026-09-16. **Discharges `AAC-0010`, currently in
`NOT_EXERCISED.md`.**

**What is missing.** A number for *how often the agent gets it right*, as opposed
to *whether it can*. Every scenario runs exactly once, against a scripted model,
so the suite is deterministic by construction: it proves the harness works and
says nothing about the agent's consistency. `AAC-0010` — variance across repeated
runs is bounded — has no test behind it and this is why.

**What the field uses.** τ-bench's `pass^k`: the probability that **all k**
trials of a task succeed, averaged over tasks. Deliberately the opposite of
`pass@k`, which asks whether *at least one* of k succeeds and suits code
generation, where you can generate ten candidates and ship whichever passes. A
support agent has no such luxury — every customer gets one attempt — so the
metric that matters is the pessimistic one.

**Why it matters.** The numbers do not degrade gently. A retail agent reported at
61% mean accuracy is at **25%** by `pass^8`. Mean accuracy hides exactly the
property a deployment cares about, and hides it worse the more the agent is used.
Frontier models sit below 70% on τ-bench `pass^1` for retail and near 46% for
airline, which is the honest shape of this problem and not a reason to avoid
measuring it.

**Where it would land.** The pieces mostly exist: `--live` in `scripts/run_view.py`
and `scripts/live_runs.py` already drive a real provider. What is absent is
running one scenario *n* times and reporting, per scenario, how many of the *n*
passed — then `pass^k` as `C(c, k) / C(n, k)` averaged across scenarios. It
belongs beside the run view rather than in the pytest suite: a reliability figure
is a report, not a gate, and a flaky test that fails 30% of the time gets
disabled within a week.

**What not to do.** Do not adopt τ-bench's 165 tasks. Different domain, and their
policy is prose in a prompt where ours is declared and machine-checkable — taking
their cases would mean giving that up. **Do** read the retail policy document as
a cross-check on the AOAS: it is 115 tasks' worth of edge cases somebody has
already thought through for a retail support agent.

## T-008 · The regeneration claim has never been run

**Status** Not started. Raised 2026-09-16. **The point of the whole family.**

**What is missing.** An experiment. The claim the spec family exists to support
is that the catalogs plus the agent specification are sufficient to *produce* an
agent — hand them to a generator in an empty folder and get similar code back.
Nothing has ever tested that. Not once, not partially.

**What is tested is the opposite direction.** 487 tests, AHC 50/72, AAC 44/52,
AOAS 50/55 — every one of them asks *does this code satisfy the specification*.
None asks *is the specification sufficient to produce this code*. A suite can be
perfect at the first and say nothing about the second, and ours is.

**The seven known weak points.** `harness-profile.yaml`'s `x_untested` lists the
capabilities believed met with no test naming them — AHC-0002, 0004, 0006, 0010,
0020, 0026, 0036. The architecture review already states what they are for: *the
best available prediction of where G1's first regeneration will disappoint,
because nothing would notice if one quietly went missing.* Closing those is worth
doing **before** the experiment, not after, or the first run produces a result
nobody can interpret.

**Where it would land.** A second directory, the specs, and a generator, with
three things fixed in advance so the result means something:

*What counts as "similar".* Not a diff. The scenarios are implementation-
independent by construction — `Subject` is three callables — so the honest
measure is **does the regenerated agent pass the same 34 scenarios and discharge
the same statements**. Structural similarity is a second, weaker question.

*The binding is part of the experiment.* `evals/simulation.py` is this agent's
binding to the scenario contract. A regeneration must supply its own, and
whether a fresh generation produces something a binding can attach to at all is
itself a finding — arguably the first one.

*What a failure teaches.* A capability the regeneration misses is a capability
the catalog states badly, not a bad generation. That is the output worth having,
and the reason to run it before the catalogs are declared finished rather than
after.

**And the result has to be read against a circularity.** The AOAS declares its
own `sources`, and two of the three are this repository — `worlds/clothing.yaml`
and `src/support_agent/{router,policy,approvals,escalation,identity,config}`.
Part of the specification was written *from* the code it would be regenerating.
So a close match is ambiguous by construction: it may mean the spec is
sufficient, or it may mean the spec was read off that exact structure. This is
not a reason to skip the experiment — it is a reason to say it out loud before
running, rather than discovering it while interpreting the result.

Two things reduce it. The **scenarios** and the **golden set** are the parts a
regeneration is honestly judged by, and neither was derived from module
structure: the golden cases are generated from declared conditions, and the
scenarios assert over a world. The parts most likely to be contaminated are the
structural ones — which is the second reason similarity is defined as passing
the scenarios rather than as a diff.

**What the spec does not constrain at all**, and is worth predicting before the
run so the prediction can be wrong: nothing declares the typed `TurnResult`
union, the conversation-as-one-blob, the five policy positions, the split of
`escalation` into wording/capacity/store/workflow, or gates running before
routing. Those came from the ratchets and the import contract in
`pyproject.toml` — which live here and not in any specification. A regeneration
in an empty folder gets none of that pressure, and if the goal is that it
should, the ratchets are a spec artifact nobody has written down.

## T-009 · Seven capabilities are believed met and named by no test

**Status** Not started. Raised 2026-09-16. **Do before T-008.**

**What is missing.** Seven `discharges` tags. `harness-profile.yaml` lists them
under `x_untested`, deliberately separated from `accepted_gaps` because the two
are different claims and this is the weaker one:

    AHC-0002  context assembly is one function, and it is inspectable
    AHC-0004  the choke point exists; the import contract is tested, the claim is not tagged
    AHC-0006  spans carry the run id across the hops
    AHC-0010  `Agent.handle` is the entrypoint scenarios drive
    AHC-0020  the fan-out limiter is bounded by the harness
    AHC-0026  the run id spans the unit of work
    AHC-0036  every tool declares an argument contract, validated at dispatch

**Why it matters.** Several are almost certainly already exercised by a test that
simply does not name them — AHC-0010 in particular, since every scenario drives
`Agent.handle`. That makes this cheap. It also makes it easy to leave, which is
why it has sat.

The cost of leaving it is stated in the architecture review: these are where a
regeneration will disappoint, *because nothing would notice if one quietly went
missing.* Until each has a test naming it, "the regeneration matched" and "the
regeneration dropped a capability we never checked" are the same observation.

**Where it would land.** Existing tests, mostly — find the test that already
proves each and add the tag. Where none exists, the gap is real and worth
knowing about before T-008 runs rather than after.

## T-010 · The constraints that produced this shape are in no specification

**Status** Not started. Raised 2026-09-16. **Blocks T-008 being interpretable.**

**What is missing.** A fourth artifact, beside AAC, AHC and AgentTwin, stating
what **shape** the code must take. The family currently says what must be TRUE,
what must EXIST and what must be FACED. It says nothing about structure — and
structure is what a regeneration would visibly differ on.

**It cannot be part of AHC, and the reason is AHC's own value.** That catalog is
stack-agnostic on purpose: the same capabilities are meant to hold whether the
harness is LangGraph, an SDK, or a hand-rolled loop. These constraints are
stack-bound. "Modules under 416 lines" means nothing to a graph; "arrows point
down only" is an import-linter concept; "only the composition root constructs a
realisation" presumes composition roots. Folding them in would smuggle one
language's opinions into a catalog that claims to transcend them, and the cost
would be paid by every reader who does not use Python. It belongs beside the
**binding** — the other stack-bound artifact — rather than inside a catalog.

**This is extraction, not design.** All of it exists, is enforced, and is green:

*The import contract* (`pyproject.toml`) — `Arrows point down` as layers with
`exhaustive = true`, so a new module cannot be added without being placed. Four
forbidden-module contracts, each failing closed with exactly one exception: only
`llm` sees a provider SDK, only `tools` speaks MCP, only `state` and
`idempotency` see psycopg, and the agent cannot see its simulator.

*The ratchets* (`tests/test_build_checks.py`) — one-way ceilings set at the worst
offender the day they were measured: complexity 14 → 8, statements 50 → 24,
branches 12 → 7, longest module 744 → 416, `type: ignore` at 2.

*The structural tests* — only the composition root constructs a realisation;
every collaborator the root takes is an interface; every failure declares its
kind; every counter declared is incremented somewhere.

*`mypy --strict`* with `exhaustive-match`, so a new route or result kind fails
the build rather than falling through the last branch.

**Why it matters, and it is the reason to do it before T-008.** The architecture
review records that the ratchets *"forced five extractions in the last two days,
each of which turned out to be a job with a name."* The module structure was not
designed — it was produced by the ceilings. So the testable claim is: a
regeneration handed the ceilings arrives at a similar structure, and one handed
only AHC does not. Without this artifact, T-008 can only report that the shapes
differ; with it, T-008 can ask whether the constraints are what carries shape.

**Open questions**, deliberately not answered here: what it is called; whether
the numbers travel (416 is this codebase's history, not a law) or only the
*mechanism* of a one-way ratchet does; and whether a TypeScript sibling would
share anything but the mechanism. The last is the test of whether this is one
artifact with bindings or a family of them.

## T-011 · Write the hotel AOAS and nothing else

**Status** Not started. Raised 2026-09-16. **Do before T-008. A day, not weeks.**

**What is missing.** Evidence that the specification set is *complete* — that
everything a second agent must change has somewhere to be said. Not evidence
that a regeneration works; that is T-008 and it is expensive. This is the cheap
falsifiable version, and it can be run without generating a line of code.

**The measurement this rests on.** `evals/reuse.py` already classifies every
module for exactly this question, and the numbers as of 2026-09-16:

    mechanism       46 files   7865 lines   77.2%   a second agent keeps the file
    parameterised    9 files   1663 lines   16.3%   keeps the code, replaces the values
    per-agent        5 files    662 lines    6.5%   writes its own

So roughly 94% of this code survives a change of domain. The five rewritten are
`contracts/domain.py`, `entrypoint/direct.py`, `approvals/refund.py`,
`escalation/wording.py`, `ui/__init__.py`.

**The exercise.** Take those five, plus the nine parameterised modules —
`router`, `approvals/policy`, `escalation/rules`, `policy`, `binding`, `config`,
`approvals`, `entrypoint/promise`, `telemetry/contract` — and write the AOAS for
a hotel support agent. Only the AOAS. Then ask, item by item: **does the format
have a place to say this?**

Where it does, that value is spec-supplied and the thesis holds for that piece.
Where it does not, a hole in AOAS has been found for the price of a day.

**Why the parameterised layer is the real subject.** Its own docstring calls it
the dangerous one: *universal code, this agent's values… a bug in the values
reads as a bug in the mechanism.* The per-agent five are obviously per-agent and
nobody will forget them. The nine are the ones that look shared and are not, and
they are where an incomplete AOAS does its damage quietly — a regenerated agent
inherits `router/__init__.py`'s shape and this agent's regexes, and passes
review.

**What this does not test**, stated so the result is not over-read: it says
nothing about whether the 77% mechanism layer would be *reproduced*. That layer
is the part no specification describes structurally — AHC constrains that a
provider choke point exists, not that it is a Protocol in the bottom layer that
adapters satisfy without importing. That gap is T-010, and this exercise is the
argument for it rather than a substitute.

**And the artifact this leans on is thin.** `evals/reuse.py` is a hand-maintained
list of filenames. Its one safeguard is that a module with no layer fails the
test, so the classification cannot rot silently — but the layer assignments
themselves are judgement, recorded once, by one reader. If the thesis rests on
that split, the split deserves more than a list.

---

## The queue

| | Item | Raised |
|---|---|---|
| T-001 | Nothing happens when the chat opens | 2026-09-05 |
| T-003 | Dedup works for one process only, and the durable port needs claim expiry | 2026-09-05 |
| T-004 | An Anthropic adapter at L2 — closes three open items, one module | 2026-09-05 |
| T-005 | Carry the idempotency key downstream — carries F-017 | 2026-09-05 |
| **T-002** | **No login exists, and the permission model cannot express ownership — carries F-016** | 2026-09-05 |
| T-006 | A customer's own conversations cannot be found — needs T-002 | 2026-09-15 |
| T-007 | Every scenario runs once, so nothing measures reliability — discharges AAC-0010 | 2026-09-16 |
| **T-008** | **The regeneration claim has never been run — the point of the whole family** | 2026-09-16 |
| T-009 | Seven capabilities believed met with no test naming them | 2026-09-16 |
| **T-010** | **The constraints that produced this shape are in no specification** | 2026-09-16 |
| **T-011** | **Write the hotel AOAS and nothing else — is the spec set complete?** | 2026-09-16 |
